# crypto-auto-trader

Upbit KRW 현물 **Long-only** 자동매매 시스템입니다. 해외 선물(Binance/Bybit/OKX) 데이터는 **읽기 전용 필터**로만 씁니다.
데이터 수집 → 저장·품질 검증 → 전략 → 이벤트 기반 백테스트 → PAPER → (승인 시) LIVE 까지 하나의 결정 경로를 공유합니다.

> 기존 주식 프로젝트(scanner_vm, myStockApp 등)와 완전히 분리된 독립 저장소입니다.
> **LIVE 주문은 기본 비활성입니다.** 3중 게이트와 서명된 승인 파일이 모두 있어야 켜집니다 (`docs/LIVE_RUNBOOK.md`).
> 백테스트 수치는 과거 시뮬레이션일 뿐 실거래 수익률이 아닙니다.

## 빠른 시작

```bash
uv sync
uv run cat probe                                   # 이 네트워크에서 각 거래소 API 접근 가능 여부
uv run cat collect-history --sources upbit --intervals 1h 15m 1d --start 2022-01-01
uv run cat collect-history --sources binance_archive --start 2022-01-01
uv run cat collect-history --sources okx --start 2026-01-01
uv run cat collect-history --sources upbit_trades --markets KRW-BTC KRW-ETH KRW-XRP --trade-days 7
uv run cat collect-live --duration 1800            # Upbit WS 체결/호가 + OKX 청산/OI/펀딩
uv run cat validate                                # .tmp 정리, 손상 파일 격리, 중복 제거
uv run cat quality-report                          # reports/data_quality.html
uv run pytest -m "not network"                     # 단위/통합 테스트 (실키 불필요)
uv run cat backtest                                # reports/backtest_report.html (+ .json)
uv run cat paper --strategy A2_sweep_choch         # 실시간 모의매매 (실주문 없음)
uv run cat dashboard                               # http://127.0.0.1:8501
uv run cat kill-switch status --mode PAPER
```

Docker: `docker compose -f docker/compose.yaml up -d` 로 collector, paper, dashboard 를 띄웁니다 (LIVE 서비스는 정의하지 않음).

## 구성

| 영역 | 위치 |
|---|---|
| 거래소 연결 (Upbit, Bithumb, Binance, Bybit, OKX) | `src/crypto_trader/collectors/` |
| Parquet 원자적 저장 · DuckDB · SQLite WAL 운영DB · 품질 | `src/crypto_trader/storage/` |
| 피처 (SMC, Order flow, 파생) — 모두 인과적 | `src/crypto_trader/features/` |
| 전략 A1–A3, B1–B3, C1–C4, 기준전략, 결합(and/filter/score) | `src/crypto_trader/strategies/` |
| 백테스트 엔진·지표·검증(IS/OOS, WF, 부트스트랩, Holm/BH, 국면, look-ahead 감사) | `src/crypto_trader/backtest/` |
| Backtest / Paper / UpbitLive 브로커 (공통 인터페이스) | `src/crypto_trader/brokers/` |
| 위험관리 · Kill Switch | `src/crypto_trader/risk/` |
| TradingCore (공통 결정 경로) · PAPER/LIVE 러너 | `src/crypto_trader/execution/` |
| 대시보드 (FastAPI, 읽기 전용) · HTML 리포트 | `src/crypto_trader/dashboard/`, `src/crypto_trader/reports/` |
| 설정 | `config/default.yaml` |
| 문서 | `docs/ARCHITECTURE.md`, `docs/API_ACCESS.md`, `docs/LIVE_RUNBOOK.md`, `docs/PHASE_REPORT.md` |

## 상태 요약

전체 상태표(IMPLEMENTED / TESTED / PARTIAL / BLOCKED / NOT IMPLEMENTED)와 실측 결과는 `docs/PHASE_REPORT.md` 에 있습니다.
