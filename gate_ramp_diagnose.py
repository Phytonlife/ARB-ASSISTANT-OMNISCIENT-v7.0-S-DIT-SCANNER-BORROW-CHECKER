#!/usr/bin/env python3
"""
gate_ramp_diagnose.py — Диагностика Gate Ramp Radar
=====================================================
Запусти ПРЯМО НА СВОЁМ DOCKER:
  python3 gate_ramp_diagnose.py

Покажет:
  1. Работает ли Gate API с твоего IP
  2. Сколько 1ч контрактов реально есть
  3. Какие монеты с отрицательным rate
  4. Правильно ли считается OI
  5. Работает ли Binance API
"""

import asyncio, json, sys

try:
    import httpx
    print("✅ httpx установлен:", httpx.__version__)
except ImportError:
    print("❌ httpx НЕ установлен!")
    print("   Запусти: pip install httpx --break-system-packages")
    sys.exit(1)

GATE_BASE    = "https://api.gateio.ws/api/v4"
BINANCE_BASE = "https://fapi.binance.com"
HEADERS = {"User-Agent": "Mozilla/5.0 Chrome/120", "Accept": "application/json"}

async def run():
    print("\n" + "="*60)
    print("  ДИАГНОСТИКА Gate Ramp Radar")
    print("="*60)

    async with httpx.AsyncClient(timeout=15, headers=HEADERS) as c:

        # ── 1. Проверяем Gate /contracts ─────────────────────────
        print("\n[1] GET /contracts (ищем 1ч монеты)...")
        r = await c.get(f"{GATE_BASE}/futures/usdt/contracts",
                        params={"limit": 100})
        print(f"  HTTP {r.status_code}")

        if r.status_code != 200:
            print(f"  ❌ Gate API недоступен: {r.text[:200]}")
            return

        contracts = r.json()
        print(f"  Всего контрактов в первых 200: {len(contracts)}")

        one_hour   = []
        eight_hour = []
        for item in contracts:
            fi   = int(item.get("funding_interval", 28800) or 28800)
            name = item.get("name","")
            rate = item.get("funding_rate","0")
            if fi == 3600:
                one_hour.append((name, rate))
            else:
                eight_hour.append((name, rate))

        print(f"  1ч контрактов:  {len(one_hour)}")
        print(f"  8ч контрактов:  {len(eight_hour)}")

        if not one_hour:
            print("\n  ❌ НЕТ 1ч контрактов — вот в чём проблема!")
            print("  Первые 5 контрактов сырые:")
            for item in contracts[:5]:
                print(f"    {item.get('name')}: interval={item.get('funding_interval')}")
        else:
            print(f"\n  Первые 10 из 1ч контрактов:")
            for name, rate in one_hour[:10]:
                rate_f = float(rate or 0)
                sign   = "🔴" if rate_f < -0.000010 else "⚪"
                print(f"    {sign} {name}: rate={float(rate or 0)*100:+.5f}%/ч")

        # Проверяем нужен ли ещё offset
        if len(contracts) == 100:
            print("\n  ℹ️  Возможно контрактов > 100, нужна пагинация")
            r2 = await c.get(f"{GATE_BASE}/futures/usdt/contracts",
                             params={"limit": 100, "offset": 100})
            if r2.status_code == 200:
                c2 = r2.json()
                print(f"  Ещё {len(c2)} контрактов (offset=100)")
                for item in c2:
                    fi   = int(item.get("funding_interval", 28800) or 28800)
                    name = item.get("name","")
                    rate = item.get("funding_rate","0")
                    if fi == 3600:
                        one_hour.append((name, rate))

        # ── 2. Монеты с отрицательным 1ч rate ───────────────────
        print(f"\n[2] Монеты с 1ч фандингом И отриц. rate:")
        neg_1h = [(n, float(r or 0)) for n, r in one_hour
                  if float(r or 0) < -0.000010]
        neg_1h.sort(key=lambda x: x[1])

        if not neg_1h:
            print("  ⚠️  Нет монет с rate < -0.001%/ч прямо сейчас")
            print("  Все 1ч монеты с любым rate:")
            for n, r in sorted(one_hour, key=lambda x: float(x[1] or 0))[:15]:
                print(f"    {n}: {float(r or 0)*100:+.6f}%/ч")
        else:
            print(f"  ✅ Найдено {len(neg_1h)} монет для watchlist:")
            for name, rate_f in neg_1h[:20]:
                print(f"    🔴 {name}: {rate_f*100:+.5f}%/ч")

        # ── 3. Gate /tickers (проверяем OI формат) ───────────────
        print("\n[3] GET /tickers (проверяем OI формат)...")
        if one_hour:
            test_contract = one_hour[0][0]
            rt = await c.get(f"{GATE_BASE}/futures/usdt/tickers",
                             params={"contract": test_contract})
            if rt.status_code == 200:
                data = rt.json()
                item = data[0] if isinstance(data, list) and data else data
                print(f"  Контракт: {test_contract}")
                print(f"  Поля ответа: {list(item.keys())}")
                mark     = float(item.get("mark_price") or item.get("last") or 0)
                idx      = float(item.get("index_price") or mark)
                total_sz = float(item.get("total_size") or 0)
                fi_from_ticker = item.get("funding_interval", "ОТСУТСТВУЕТ")
                oi_usdt  = total_sz * mark

                print(f"  mark_price:       {mark}")
                print(f"  index_price:      {idx}")
                print(f"  total_size:       {total_sz}")
                print(f"  funding_interval: {fi_from_ticker}  ← {'❌ нет в tickers (это нормально, берём из /contracts)' if fi_from_ticker == 'ОТСУТСТВУЕТ' else '✅'}")
                print(f"  OI (size×mark):   ${oi_usdt/1e6:.2f}M")
                dev = (mark-idx)/idx*100 if idx > 0 else 0
                print(f"  Premium:          {dev:+.4f}%")

        # ── 4. Binance API ────────────────────────────────────────
        print("\n[4] Binance API...")
        if neg_1h:
            sym_test = neg_1h[0][0].replace("_USDT", "USDT")
            rb = await c.get(f"{BINANCE_BASE}/fapi/v1/premiumIndex",
                             params={"symbol": sym_test})
            print(f"  HTTP {rb.status_code} для {sym_test}")
            if rb.status_code == 200:
                d = rb.json()
                mark = float(d.get("markPrice",0))
                idx  = float(d.get("indexPrice",0))
                dev  = (mark-idx)/idx*100 if idx > 0 else 0
                print(f"  ✅ Binance premium {sym_test}: {dev:+.4f}%")
            else:
                print(f"  ❌ Binance ответил: {rb.text[:100]}")

        # ── 5. Итог и рекомендации ────────────────────────────────
        print("\n" + "="*60)
        print("  ИТОГ И РЕКОМЕНДАЦИИ")
        print("="*60)

        if not one_hour:
            print("""
  ❌ ПРОБЛЕМА: Gate не вернул 1ч контракты
  
  Вероятные причины:
  a) Gate API возвращает funding_interval в другом поле
     Проверь: поля первых контрактов выше
  b) Пагинация: все 1ч контракты после первых 200
     Уже проверено выше
  c) Gate API временно не работает
""")
        elif not neg_1h:
            print(f"""
  ⚠️  ПРОБЛЕМА: 1ч контрактов {len(one_hour)} штук, но rate НЕ отрицательный
  
  Это НОРМАЛЬНО если сейчас нет разгона монет с 1ч фандингом.
  
  Что делать:
  1. Снизить порог GATE_RATE_NEG_THRESHOLD в gate_ramp_radar.py
     Текущий: -0.000010 (= -0.001%/ч)
     Попробуй: 0.0 (показывать все 1ч монеты независимо от знака)
  
  2. Ждать — разгоны не происходят постоянно
  
  Все 1ч монеты с любым rate:
""")
            for n, r in sorted(one_hour, key=lambda x: float(x[1] or 0))[:20]:
                print(f"    {n}: {float(r or 0)*100:+.6f}%/ч")
        else:
            print(f"""
  ✅ Gate API работает
  ✅ {len(one_hour)} монет с 1ч фандингом
  ✅ {len(neg_1h)} монет с отрицательным rate → должны быть в watchlist
  
  ЕСЛИ WATCHLIST ВСЁ ЕЩЁ ПУСТОЙ:
  Проблема в коде внедрения. Проверь main.py:
  
  # ПРАВИЛЬНО:
  async def post_init(app):
      async def send_fn(cid, text):
          await app.bot.send_message(cid, text)
      CHAT_ID = str(update.effective_chat.id)  # свой chat_id!
      app.bot_data["gate_task"] = asyncio.create_task(
          run_gate_ramp_radar(send_fn, CHAT_ID)
      )
  
  # ЧАСТАЯ ОШИБКА: chat_id как int вместо str
  # ЧАСТАЯ ОШИБКА: send_fn не async
  # ЧАСТАЯ ОШИБКА: CHAT_ID = 0 или пустой
""")

asyncio.run(run())
