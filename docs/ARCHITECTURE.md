# 아키텍처

```
                 ┌──────────── collectors (async, read-only) ────────────┐
 Upbit REST/WS ──┤ upbit/rest.py  upbit/ws.py (reconnect, seq backfill)  │
 Bithumb REST ───┤ bithumb/rest.py                                       │──► storage/parquet_store.py
 Binance archive ┤ binance/archive.py (checksum, format normalisation)   │    (atomic publish, partitions)
 Binance fapi ───┤ binance/futures_rest.py (451 -> AccessBlocked)        │            │
 Bybit V5 ───────┤ bybit/rest.py, bybit/ws.py (allLiquidation)           │            ▼
 OKX V5 ─────────┤ okx/client.py (OI, funding, liquidation WS)           │     storage/duck.py (DuckDB views)
                 └───────────────────────────────────────────────────────┘     storage/quality.py
                                                                                         │
     features/ (causal numpy/polars)  ◄──────────────── backtest/dataset.py ◄────────────┘
       common.py  smc.py  orderflow.py  derivatives.py (as-of by available_ts)
                     │
     strategies/base.py  (prepare(features) / on_bar(i) — stateless in time)
     strategies/library.py  A1–A3, B1–B3, C1–C4, BASE, Combined(and/filter/score)
                     │
     execution/core.py  TradingCore  ── risk/manager.py (sizing, limits) ── risk/kill_switch.py
                     │                                                       (persisted in ops DB)
          ┌──────────┼───────────────────┐
  brokers/backtest.py  brokers/paper.py   brokers/upbit_live.py (+ LiveGuard, reconcile)
  (next-open fills,    (walks live book)  (identifier idempotency, UNKNOWN handling)
   participation cap)
          │                  │                     │
   backtest/engine.py   execution/runner.py (PAPER / LIVE loop)      storage/ops_db.py (SQLite WAL)
   backtest/research.py                                               orders, fills, positions, kv,
   backtest/validation.py                                             events, signals, equity
          │                                                                │
   reports/backtest_report.py, reports/quality_report.py        dashboard/app.py (FastAPI, read-only)
```

## 핵심 설계 결정

1. **하나의 결정 경로.** BACKTEST, PAPER, LIVE 가 모두 `TradingCore.on_bar_close → RiskManager.check_entry → Broker.submit` 을 거칩니다.
   전략은 `prepare` 로 인과적(causal) 피처 배열을 만들고 `on_bar(i)` 로 판단합니다. 실시간에서는 최근 윈도우로 `prepare` 를 다시 돌리고 마지막 봉에서 `on_bar` 를 부릅니다.
   `tests/test_modes_restart.py::test_same_decision_function_live_window_vs_backtest` 가 두 결과가 같은지 확인합니다.
2. **실행 가격은 Upbit만.** 진입, 손절·익절, ATR, 스윙, FVG 등은 모두 Upbit KRW 봉에서 계산합니다. 해외 선물 데이터는
   `d_*`, `oi_*`, `funding_z`, `fut_*` 컬럼으로 붙고 필터·국면 판단에만 씁니다.
3. **시각 4종.** `event_ts`(거래소), `received_ts`(수신), `available_ts`(전략이 쓸 수 있는 시각), `decision_ts`(signals 테이블)을 모두 기록합니다.
   거래소가 다른 데이터는 `available_ts` 기준 as-of backward join 으로만 붙입니다. Binance metrics 는 보수적으로 `create_time + 5분`, 봉은 close 시각에 사용 가능한 것으로 봅니다.
4. **SMC 확정 시점.** 스윙은 우측 N봉이 끝난 봉에서야 알 수 있는 값으로 인덱싱합니다. 같은 봉에서 확정된 스윙을 사용하지 않고 직전 봉(`i-1`) 기준 레벨을 씁니다.
5. **체결 모델.** 봉 종가에서 결정하고 다음 봉 시가에 체결합니다. 편도 슬리피지는 `왕복비용/2 − 수수료` 입니다. 봉 거래대금의 5% 를 넘는 부분은 부분 체결로 처리하고 나머지는 취소합니다.
   한 봉 안에서 손절과 익절이 모두 닿으면 손절로 처리합니다. 갭이면 시가에 체결합니다. 실시간 호가를 쓰는 PAPER 는 호가를 직접 걷기 때문에 스프레드를 따로 빼지 않습니다(중복 차감 방지).
6. **Long-only 강제.** `SimBroker` 는 보유 수량을 넘는 매도를 거부합니다(`insufficient_funds_ask`). 리스크 매니저는 같은 종목 추가 매수(물타기)를 거부합니다.
7. **운영 상태는 SQLite WAL.** 주문, 체결, 포지션, Kill Switch, 위험 상태, 감사 로그를 저장합니다. 모드별 DB 파일을 분리하고 `mode` 컬럼으로 이중 격리합니다.
8. **원자적 게시.** Parquet 은 `.tmp-*.parquet.tmp` 로 쓰고 fsync 한 뒤 다시 읽어 검증하고 `os.replace` 로 게시합니다. 독자는 `*.parquet` 만 보고, 숨김 파일은 무시합니다.
   `validate()` 는 남은 임시 파일을 지우고, 손상 파일은 `quarantine/` 으로 옮기고, 중복은 압축 정리합니다.
9. **차단은 우회하지 않음.** 451 이나 국가 403 은 `AccessBlocked` 로 올리고 재시도하지 않습니다. 대체 공급원(아카이브, OKX)을 씁니다.

## 디렉터리

`src/crypto_trader/` 아래에 요청된 구조(collectors, storage, features, strategies, backtest, brokers, risk, execution,
dashboard, reports)를 그대로 따랐습니다. 패키지 import 충돌을 피하려고 최상위 대신 `src/` 레이아웃을 썼습니다.
`storage/` 의 parquet, duckdb, operations 는 각각 `parquet_store.py`, `duck.py`, `ops_db.py` 입니다.

## 위험 관리 공식

- 거래당 위험 = `min(30,000, 할당 평가자산 × 1.5%) × size_scale`
- 일간 손실 한도 = `min(60,000, 당일 KST 시작 평가자산 × 3%)` — KST 자정에 일간 값만 초기화합니다.
- `size_scale`: 연속 3회 손실이면 ×0.5. 고점 대비 낙폭이 5% 를 넘으면 선형으로 줄이고 하한은 0.25 입니다.
- 수량 = 위험예산 / (진입가 − 손절가 + 진입가 × 왕복비용). 현금, 노출 한도(미체결 포함), 거래자금 한도로 다시 제한합니다. 5,000원 미만은 거부합니다.
- Kill Switch 사유는 두 종류입니다.
  - 자동 해제되지 않는 사유: `CUM_LOSS`, `TOTAL_LOSS`, `MANUAL`, `BALANCE_MISMATCH`, `ORDER_STATE_MISMATCH`, `UNKNOWN_ORDER_STATE`, `ABNORMAL_ORDERS`, `DB_WRITE_FAILURE`
  - 일시 사유(복구되면 자동 해제): `STALE_MARKET_DATA`, `ABNORMAL_SPREAD`, `STALE_DERIVATIVES`, `CLOCK_SKEW`, `API_ERROR`
