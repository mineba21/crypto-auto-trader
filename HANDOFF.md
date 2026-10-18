# HANDOFF — crypto-auto-trader

다른 에이전트(Codex 등)나 사람이 이 프로젝트를 이어받을 때 읽는 문서입니다.
기준 시점: **2026-10-18 (KST)**. 이 문서 이후의 변경은 `git log --oneline` 으로 확인하세요. 상세 근거는 `docs/` 에 있습니다.
**파일·데이터가 어디 있는지는 9절 "파일 위치"** 를 보세요.

---

## 1. 한 줄 요약

Upbit KRW **현물 Long-only** 자동매매 시스템. 해외 선물(Binance/Bybit/OKX)은 **읽기 전용 필터**로만 씁니다.
수집 → 저장·품질 → 전략 → 이벤트 백테스트 → PAPER → (승인 시) LIVE 가 **하나의 결정 경로**(`TradingCore`)를 공유합니다.
**현재 운영자 Mac 에서 4주 PAPER(모의매매)가 진행 중**이고, LIVE 는 꺼져 있습니다.

## 2. 절대 규칙 (바꾸지 말 것)

1. **LIVE 를 켜지 않는다.** LIVE 는 3중 게이트(`mode: LIVE` + `live.enabled` + `CAT_ENABLE_LIVE=I_ACCEPT_REAL_MONEY_RISK`)와
   HMAC 서명된 `config/live_approval.json` 이 모두 있어야 동작합니다. 운영자의 명시적 승인 없이 이 중 무엇도 만들거나 바꾸지 않습니다.
2. **Long-only.** 공매도·레버리지·해외 선물 주문 금지. 매도는 보유 수량 청산에만 씁니다.
3. **실행가격은 Upbit 데이터만** 씁니다. 해외 선물 데이터는 필터·국면 판단에만 사용합니다.
4. **Look-ahead 금지.** 모든 피처는 인과적(output[i] 는 input[..i] 만 사용)이고, 외부 데이터는 `available_ts` 기준 as-of join 으로만 붙입니다.
   스윙은 우측 N봉 확정 후에만 사용합니다. `tests/test_backtest.py::test_strategies_have_no_lookahead`, `test_features`, `test_alignment` 가 지키고 있습니다.
5. **차단된 API 는 우회하지 않는다** (HTTP 451 / 국가 403 → `AccessBlocked`). 프록시·VPN 우회 코드를 만들지 않습니다.
6. **데이터를 만들어 내지 않는다.** 없는 호가·청산·체결을 합성하지 않고, 공백은 `gaps` 로 기록합니다.
7. **결과를 부풀리지 않는다.** 나쁜 전략도 리포트에 남깁니다. 파라미터를 과최적화하지 않고, 새 파라미터는 walk-forward / OOS 로만 평가합니다.
8. **비밀값을 출력·커밋하지 않는다.** `.env` 는 gitignore 대상이고, 텔레그램 토큰은 로그에 찍히지 않게 처리돼 있습니다(httpx 로그 WARNING).
9. 기존 주식 프로젝트(stocks, scanner_vm, myStockApp)와 코드·DB 를 공유하지 않습니다. 이 저장소는 독립 프로젝트입니다.

## 3. 현재 상태

### 운영 중 (운영자 MacBook Air, Docker Desktop, 2026-10-18 시작)

`docker compose -f docker/compose.yaml` 로 서비스 3개가 돌고 있습니다.

| 서비스 | 명령 | 내용 |
|---|---|---|
| `collector` | `collect-live --duration 0` | Upbit 체결·호가(WS), OKX OI·펀딩추정·mark(REST 60초), 강제청산 WS(OKX + **Binance·Bybit — 아래 주의**). 1시간마다 지난 날짜 parquet 압축 |
| `paper` | `paper-fleet` | 10개 인스턴스: A2_sweep_choch / BASE_ema_trend / BASE_donchian × KRW-BTC/ETH/XRP + B1_short_squeeze/KRW-BTC. 각 가상 300,000 KRW |
| `dashboard` | `dashboard` | http://127.0.0.1:8501 (읽기 전용) |

- 한국 회선 `probe` 결과: **Binance fapi OK, Bybit v5 OK** (미국 클라우드에서는 둘 다 BLOCKED 였음). B1 실시간 파생 피드 정상.
- ⚠️ **Binance·Bybit 청산 수집 코드는 `main` 에 들어갔지만, Mac 에 아직 적용되지 않았을 수 있습니다.**
  Mac 에서 `git pull && scripts/mac_start.sh` 를 실행하기 전까지 collector 는 OKX 청산만 받습니다.
  적용 여부는 `docker compose -f docker/compose.yaml logs collector | grep "liquidation sources"` 로 확인합니다
  (`'binance_fapi': 'OK', 'bybit_v5': 'OK'` 가 보이면 적용된 것).
- 운영 DB 는 Docker 볼륨 `docker_ops`(`/app/data/ops`) 에 있습니다. 시장 데이터는 Mac 의 `./data`.
- 알림: 텔레그램(기존 myStockApp `.env` 에서 키 가져옴). healthchecks.io 는 **아직 미설정 (운영자가 나중에 하기로 보류)**. 방법은 `docs/MAC_SETUP.md` §1-4.
- 일정: 매일 09:00 KST 일일 요약, 월요일 09:10 KST 주간 `paper_report`, 4주 뒤 `paper-report --days 28` 로 합격 판정.

### 연구 결과 (2022-01 ~ 2026-10, 1h, 왕복 비용 0.30%) — `docs/PHASE_REPORT.md`

- **통계적으로 유의한 우위가 있는 전략은 없음.** Holm 보정 최소 p=0.71 (42개 조합).
- SMC(A1–A3): 3개 시장 · 3개 비용 시나리오 · walk-forward 모두에서 음의 기대값.
- 추세 기준전략(EMA, Donchian): 개발 구간(2022–24)에 이익 집중, OOS 는 엇갈림. Donchian WF OOS +18.4%.
- 체결 기반 C 계열: 데이터가 7일뿐이라 판단 불가. 과거 호가·청산 데이터가 없어 C4 와 B2 원설계는 검증 불가.
- 따라서 **LIVE 후보 없음.** 이번 4주는 시스템 안정성 검증과 데이터 축적이 목적입니다.

## 4. 코드 지도 (`src/crypto_trader/`)

| 경로 | 내용 |
|---|---|
| `core/` | 모델(`Bar`, `Order`, `Signal`, …), 설정(`config.py`, YAML + pydantic), 시간(UTC 저장, KST 표시) |
| `collectors/` | `upbit/rest.py`, `upbit/ws.py`(재연결·seq 백필·gap 기록), `binance/archive.py`(data.binance.vision, 포맷 변경 처리), `binance/futures_rest.py`, `binance/live_feed.py`(아카이브+REST → 아카이브 스키마), `binance/liquidations.py`(`!forceOrder@arr`), `bybit/rest.py`, `bybit/ws.py`(`allLiquidation`), `okx/client.py`(OI·펀딩·청산), `liquidation_stream.py`(Binance·Bybit 공용 WS 루프, 재연결 gap 기록), `bithumb/`, `jobs.py`(probe·history·live) |
| `storage/` | `parquet_store.py`(파티션, `.tmp-*.parquet.tmp` → `os.replace` 원자적 게시, validate, compact), `ops_db.py`(SQLite WAL, `mode` 네임스페이스), `duck.py`, `quality.py` |
| `features/` | `common.py`, `smc.py`, `orderflow.py`(CVD·OBI), `derivatives.py`(as-of 정렬, OI·funding z, `liquidation_bars` 1시간 청산 합계) |
| `strategies/` | `base.py`(`prepare` / `on_bar` — 시간에 대해 stateless), `library.py`(A1–A3, B1–B3, C1–C4, BASE, `Combined` and/filter/score) |
| `execution/` | `core.py` **TradingCore**(모든 모드 공통), `runner.py`(LiveSession, `run_paper_fleet`, `run_live`) |
| `brokers/` | `base.py` 인터페이스, `sim.py`, `backtest.py`(다음 봉 시가, 참여율 상한 부분체결), `paper.py`(실호가 walk), `upbit_live.py`(LiveGuard, identifier 멱등성, UNKNOWN 처리, reconcile) |
| `risk/` | `manager.py`(거래당·일간 한도, 보유·미체결 위험 반영, 연속손실·DD 축소), `kill_switch.py`(DB 영속, parent 전역 스위치, refresh, TRIGGER_HOOKS) |
| `backtest/` | `engine.py`, `metrics.py`, `validation.py`(bootstrap, Holm/BH, WF, 국면, look-ahead 감사), `dataset.py`(Upbit 봉 + 파생 + 청산 `long_liq_usd`/`short_liq_usd` 결합), `research.py` |
| `ops/` | `notify.py`(텔레그램·ping), `watchdog.py`, `disk.py`, `fleet_ops.py`(FleetOps / CollectorOps) |
| `reports/` | `backtest_report.py`, `quality_report.py`, `paper_report.py`(가동률·신호 일치·슬리피지·스프레드·공백), `html.py` |
| `dashboard/app.py` | FastAPI, `/api/market`, `/api/account?mode=`, `/api/instances`, `/api/strategy` |
| `cli.py` | `uv run cat <command>` 진입점 |

**PAPER 인스턴스 격리:** 하나의 SQLite 파일에서 `mode="PAPER:<strategy>:<market>"` 로 나눕니다.
전역 Kill Switch 는 `mode="PAPER"` 이고, 각 인스턴스 스위치의 parent 입니다.

## 5. 명령

```bash
uv sync
uv run pytest -m "not network"        # 109 passed (2026-10-18 기준, 실키·네트워크 불필요)
uv run pytest -m network              # 2개: 실 Upbit API, Binance 차단/허용 감지
uv run ruff check src tests
uv run cat probe                      # 이 네트워크에서 각 거래소 API 접근 여부
uv run cat collect-history --sources upbit binance_archive okx upbit_trades
uv run cat collect-live --duration 0  # 0 = 무기한 (서비스 모드)
uv run cat validate                   # .tmp 정리, 손상 격리, 중복 제거
uv run cat compact                    # 지난 날짜의 작은 parquet 병합
uv run cat quality-report             # reports/data_quality.html
uv run cat backtest [--fast]          # reports/backtest_report.html + .json (약 7분)
uv run cat paper-fleet [--only KRW-BTC] [--duration 0]
uv run cat paper-report --days 7
uv run cat notify-test
uv run cat kill-switch on|off|status --mode PAPER
```

운영자 Mac 에서는 `scripts/mac_start.sh` 하나로 빌드·점검·시작까지 합니다. 다시 실행해도 안전합니다(데이터·상태 유지).
컨테이너 안에서 실행할 때: `docker compose -f docker/compose.yaml exec paper uv run --no-sync cat <command>`.

## 6. 알려진 함정

- **Binance 아카이브:**
  - spot klines 는 2025-01부터 마이크로초 타임스탬프에 헤더가 없습니다.
  - metrics 에 빈 필드("")가 있어 비엄격 cast 를 씁니다.
  - fundingRate 는 월 단위로 게시돼 당월분이 없습니다(→ 실시간은 REST).
  - liquidationSnapshot 은 404 (제공 안 함).
- **Upbit:**
  - 체결 REST 는 최근 7일만 제공합니다.
  - 무체결 구간에는 캔들이 없습니다(결측 ≠ 수집 실패).
  - `sequential_id` 는 연속 정수가 아니고, 같은 ms 안에서 순서가 바뀌어 올 수 있습니다(→ 최근 ID 집합으로 중복 제거).
  - **서버측 스톱 주문이 없습니다** → 손절은 클라이언트가 감시하므로 프로세스가 죽으면 손절도 멈춥니다.
- `notify.send` 는 텔레그램이 설정돼 있으면 로그에 "notify sent:" 만 남깁니다(`a1e599d` 이전에는 아무것도 남지 않았음).
- `pgrep -f "<문자열>"` 은 자기 셸 명령줄에도 매칭됩니다(대기 루프 작성 시 주의).
- **개발 클라우드 컨테이너**는 미국 리전(Binance/Bybit 차단)이고, 프록시 자체 CA 를 씁니다.
  docker build 는 CA 를 주입해야 하지만, 운영자 Mac 에서는 필요 없습니다.
- Docker Desktop for Mac: SQLite WAL 은 bind mount 대신 Docker 볼륨(`ops`)에 둡니다.
- **강제청산 방향 표기가 거래소마다 다릅니다** (모두 `side = long_liq | short_liq` 로 정규화):
  - Binance `o.S=SELL` → `long_liq` (강제 매도 = 롱 청산)
  - Bybit `S=Buy` → `long_liq`. 공식 문서: *"When you receive a Buy update, this means that a long position has been liquidated"*.
    초기 구현이 반대로 돼 있었고, 운영에 연결되기 전에 수정했습니다.
  - OKX `posSide=long` → `long_liq`
- 청산 합계는 **하한값**입니다. Binance 는 심볼당 1초에 마지막 1건만, Bybit 는 500ms 스냅샷만 보냅니다.
  수집하지 않은 시간은 `liquidation_bars` 가 0 이 아니라 **null(미상)** 로 둡니다(모든 소스가 기록 중이던 구간만 유효).
- `reports/backtest_report.json` 은 생성물이라 git 에서 제외했습니다. 대시보드 전략 패널은 이 파일이 없으면 `reports/samples/backtest_report.json`(2026-10-17 스냅샷)을 읽습니다.

## 7. 다음 할 일 (우선순위)

1. **[운영자 Mac 에 적용 필요] 청산 데이터 수집 확대 — 코드 완료.**
   - Binance·Bybit 강제청산 WS 수집, Bybit 방향 버그 수정, 대시보드 합산, 백테스트용 1시간 청산 피처까지 `main` 에 반영돼 있습니다.
   - 운영자가 Mac 에서 `git pull && scripts/mac_start.sh` 를 실행해야 수집이 시작됩니다. 데이터·모의 계좌 상태는 유지됩니다.
     과거 청산 데이터는 어디에도 없으므로 **적용이 늦어지는 만큼 데이터가 영구히 비게 됩니다.**
   - 적용 후 확인할 것:
     - collector 로그의 `liquidation sources: {... 'binance_fapi': 'OK', 'bybit_v5': 'OK'}`
     - 몇 시간 뒤 `data/market/kind=liquidations/exchange=binance|bybit/` 에 파일이 생기는지
     - 일일 요약의 `liq_binance=`, `liq_bybit=` 행 수
   - 개발 클라우드(미국)에서는 두 소스가 차단돼 **실제 스트림으로는 검증하지 못했습니다.**
     검증은 공식 문서 기준 파서 테스트와 로컬 가짜 WS 서버 테스트(`tests/test_liquidations.py`)까지입니다.
2. **[완료]** `docs/API_ACCESS.md` 에 한국 회선 probe 결과와 거래소별 청산 표기 차이를 반영했습니다.
3. healthchecks.io 는 운영자가 나중에 설정하기로 보류했습니다. 설정 전까지는 Mac 이 잠들어도 알림이 없습니다. 운영자가 요청하면 `docs/MAC_SETUP.md` §1-4 대로 안내하고, `notify-test` 에서 `ping ok` 두 줄을 확인합니다.
4. Upbit 개인 주문 이벤트 WebSocket(myOrder)을 구현합니다 (LIVE 준비용, 현재 REST 조회·reconcile 로 대체).
5. GitHub Actions CI (pytest + ruff).
6. 4주 뒤:
   - `paper-report --days 28` 합격 기준을 확인합니다: 가동률 ≥ 99%, 신호 일치 100%, 실측 슬리피지 ≤ 편도 10bps, 원인 불명 Kill Switch 0건.
   - 쌓인 체결·호가·청산으로 C 계열과 B2 를 사전 고정 파라미터·walk-forward 로 재평가합니다.
     B2 는 `B2LiquidationReversal(require_liquidations=True)` 로 돌립니다. `liq_min_usd` 기본값 5e6 은 BTC 기준 가정이라,
     **데이터를 보기 전에** 종목별 기준을 정해 두세요(사후 조정 금지).
   - (선택) PAPER 에 B2 인스턴스를 추가하려면 `execution/runner.py::LiveSession.frame()` 에 `attach_liquidations` 를 연결해야 합니다.
     인스턴스 추가는 실험 중간 변경이므로 4주 실험이 끝난 뒤에 합니다.
7. (선택) Tardis.dev 어댑터: Upbit 체결·호가 2021-03~ 이력(유료, 매월 1일 무료 샘플)으로 C 계열을 장기 검증합니다.

## 8. 작업 방식 메모

- 커밋 작성자는 `mineba21 <hyegang21@gmail.com>` 를 써 왔습니다. 브랜치를 따로 만들 때는 운영자 확인 후 `main` 에 병합해 왔습니다.
- 변경 후에는 반드시 `pytest -m "not network"` 와 `ruff` 를 통과시킨 뒤 push 합니다.
- 운영 중인 Mac 서비스에 영향을 주는 변경(스키마, `config/default.yaml` 의 `paper_instances`, ops DB 키)은 **4주 실험의 연속성을 깨지 않도록** 하위호환을 유지합니다.
  인스턴스 mode 이름이 바뀌면 이력이 끊깁니다.
- 운영자 선호: Python 엔지니어이고 LLM·이상감지에 관심이 많습니다. 설명은 한국어로 합니다.

## 9. 파일 위치

### 저장소
| 무엇 | 위치 |
|---|---|
| GitHub | https://github.com/mineba21/crypto-auto-trader (기본 브랜치 `main`) |
| 운영자 Mac 의 작업 폴더 | `~/Desktop/claude/crypto-auto-trader` (MAC_SETUP 안내 기준. 실제 경로는 운영자 확인) |
| 개발용 클라우드 컨테이너 | `/home/user/crypto-auto-trader` — 임시 환경이라 세션이 끝나면 사라집니다. 기준은 항상 GitHub `main` 입니다 |

### 운영자 Mac (저장소 폴더 기준 상대경로)
| 무엇 | 위치 | 비고 |
|---|---|---|
| 비밀 설정 | `.env` | gitignore. 텔레그램·healthcheck 키. 형식은 `.env.example` |
| 텔레그램 키 원본 | `/Users/soohan/Desktop/claude/test1/myStockApp-scanner-vm/.env` | `scripts/import_telegram_env.sh` 가 TELEGRAM 키만 복사. 다른 프로젝트 파일이므로 수정 금지 |
| 시장 데이터 (Parquet) | `data/market/kind=<종류>/exchange=<거래소>/symbol=<심볼>/date=<YYYY-MM-DD>/*.parquet` | 종류: ohlcv, trades, orderbook, open_interest, funding, mark_price, liquidations, metrics, premium_index, gaps |
| Binance 아카이브 캐시 | `data/cache/binance/` | data.binance.vision zip 원본 |
| 손상 파일 격리 | `data/quarantine/` | `cat validate` 가 옮김 |
| 운영 DB (SQLite WAL) | Docker 볼륨 `docker_ops` → 컨테이너 안 `/app/data/ops/` | `ops_paper.sqlite`(PAPER 인스턴스 10개 + 전역 `PAPER`), `ops_collector.sqlite`. Mac 의 `data/ops/` 는 볼륨에 가려져 쓰이지 않음 |
| 리포트 | `reports/` | `paper_report_YYYYMMDD.html`(주간 자동), `probe_latest.txt`(mac_start 5단계), `backtest_report.html/.json`·`data_quality.html`(명령 실행 시 생성). 모두 git 제외 |
| 리포트 커밋본 | `reports/samples/` | 2026-10-17 백테스트·품질 리포트 사본 (git 추적, 대시보드 전략 패널의 기본값) |
| 서비스 로그 | `docker compose -f docker/compose.yaml logs <collector\|paper\|dashboard>` | json-file, 서비스당 20MB × 5개 회전 |
| 대시보드 | http://127.0.0.1:8501 | Mac 안에서만 접근 |

### 설정·실행 파일
| 무엇 | 위치 |
|---|---|
| 기본 설정 (PAPER 인스턴스 목록 포함) | `config/default.yaml` |
| LIVE 설정 예시 (사용 금지) | `config/live.example.yaml` — `config/live.yaml`, `config/live_approval.json` 은 존재하지 않아야 정상 |
| Docker | `docker/Dockerfile`, `docker/compose.yaml` |
| 스크립트 | `scripts/mac_start.sh`(원샷 시작), `scripts/import_telegram_env.sh`, `scripts/phase1_2.sh`(Phase 1–2 재현), `scripts/approve_live.py`(LIVE 승인 — 운영자 전용) |
| 문서 | `docs/ARCHITECTURE.md`, `docs/API_ACCESS.md`, `docs/PHASE_REPORT.md`, `docs/MAC_SETUP.md`, `docs/LIVE_RUNBOOK.md`, `docs/generated/api_probe_results.json` |
| 에이전트 지침 | `AGENTS.md`(요약 규칙), 이 파일 `HANDOFF.md` |

### 청산 수집 관련 파일
| 파일 | 역할 |
|---|---|
| `src/crypto_trader/collectors/liquidation_stream.py` | Binance·Bybit 공용 WS 루프 (재연결 백오프, `gaps` 기록, 심볼 필터) |
| `src/crypto_trader/collectors/binance/liquidations.py` | `!forceOrder@arr` 파서·수집 |
| `src/crypto_trader/collectors/bybit/ws.py` | `allLiquidation.{symbol}` 파서·수집 (20초 앱 ping) |
| `src/crypto_trader/collectors/okx/client.py` | OKX `liquidation-orders` (기존) |
| `src/crypto_trader/collectors/jobs.py::collect_live` | 시작할 때 접근 확인 → 가능한 소스만 실행 |
| `src/crypto_trader/ops/fleet_ops.py::CollectorOps` | 일일 통계 `liq_binance`/`liq_bybit`, 스트림 gap 텔레그램 알림 |
| `src/crypto_trader/features/derivatives.py::liquidation_bars`, `backtest/dataset.py::attach_liquidations` | 1시간 청산 합계(커버리지 밖은 null) → 봉에 as-of 결합 |
| `tests/test_liquidations.py`, `tests/test_parsing.py` | 파서 방향, 가짜 WS 서버 재연결·gap, 커버리지·인과성 |
