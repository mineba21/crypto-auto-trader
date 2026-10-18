# Phase 보고서 (2026-10-17)

상태 표기: **IMPLEMENTED** 구현 · **TESTED** 자동 테스트 또는 실데이터 실행으로 검증 · **PARTIAL** 일부 · **BLOCKED** 외부 환경·데이터·권한 문제 · **NOT IMPLEMENTED** 미구현

재현 기준: git `c09c4ba` · config `default.yaml` · 데이터 2022-01-01 ~ 2026-10-17 · 할당금 300,000 KRW · 왕복 비용 0.16 / 0.30 / 0.50 %.
실거래 성과는 **검증되지 않았습니다**. 아래 수익률은 모두 과거 데이터 시뮬레이션입니다.

---

## 1. 구현 기능과 상태

| 항목 | 상태 | 근거 |
|---|---|---|
| Upbit REST (마켓, 캔들, 체결, 호가, 현재가, 호가단위) | TESTED | 실 API 수집 + `test_parsing`, `test_network` |
| Upbit WebSocket 체결·호가, 재연결, 체결 누락 백필, gap 기록 | TESTED | 30분 실수집 + `test_ws_collector` |
| Upbit 개인 API (잔고, 주문 생성·취소·조회, orders/test, JWT) | IMPLEMENTED · Mock TESTED | `test_upbit_live` (MockTransport). **실키 호출 없음** |
| Upbit 개인 주문 이벤트 WebSocket (myOrder) | NOT IMPLEMENTED | 지금은 REST 조회와 reconcile 로 대신함 |
| Bithumb 공개 데이터 / 공통 인터페이스 | PARTIAL | 공개 REST만 구현. 실거래 NOT IMPLEMENTED |
| Binance USD-M REST 클라이언트 | IMPLEMENTED · **BLOCKED** | 이 컨테이너(미국)에서 HTTP 451 |
| Binance 공식 아카이브 (klines, metrics/OI, funding, premium) | TESTED | 2022-01 ~ 2026-10-15 수집. 체크섬 검증 실패 0, 파싱 실패 0 |
| Bybit V5 REST / allLiquidation WS | IMPLEMENTED · **BLOCKED** | HTTP 403 국가 차단. 이 지역에서는 수집하지 않음 |
| OKX OI, funding(확정·추정), mark, 청산 WS | TESTED | 실수집: OI 1h 60일, funding 96일, 청산 4건 |
| KRW-USDT 수집 / `usdt_adjusted_spread` | TESTED | 2024-06-07 상장 이후. 대시보드에 표시. FX 프리미엄 아님 |
| `fx_adjusted_premium` | PARTIAL | 함수만 있음. FX 공급원이 없어 계산하지 않음 |
| Parquet 파티션·원자적 게시·검증·격리·압축 | TESTED | `test_storage`, `cat validate` (파일 33,480개, 손상 0) |
| SQLite WAL 운영 DB (모드 격리) | TESTED | `test_modes_restart`, `test_kill_switch` |
| 데이터 품질 리포트 | TESTED | `reports/data_quality.html` |
| SMC 피처 (swing 확정 시점, sweep, FVG, CHoCH) | TESTED | `test_features` (확정 지연 · 인과성) |
| Order flow (CVD, delta, normalized delta, divergence, OBI) | TESTED | `test_features` |
| 파생 피처 (OI 변화·백분위, funding z, premium z, taker) | TESTED | `test_alignment` (available_ts as-of, 인과성) |
| 전략 A1·A2·A3 | TESTED | 실데이터 백테스트 + look-ahead 감사 PASS |
| 전략 B1·B3 | TESTED | 실데이터(Binance 아카이브) 백테스트 |
| 전략 B2 (청산 확인 포함 원설계) | **BLOCKED (데이터)** | 과거 청산 데이터 없음. "청산 확인 없음" 버전만 백테스트 |
| 전략 C1–C3 (CVD) | PARTIAL | 실 체결 7일분(5분봉 2,097개)만 있음 → 통계적 판단 불가 |
| 전략 C4 (OBI) | PARTIAL / **BLOCKED (데이터)** | 실 호가 30분분만 있음. 과거 호가 아카이브 없음 |
| 결합 A+B, A+C (and/filter/score), ablation | TESTED | 3절 |
| 결합 B+C, A+B+C | **BLOCKED (데이터)** | 체결 구간(10-10~17)에 Binance funding 아카이브 미게시(10월분), Binance REST 차단 |
| 이벤트 기반 백테스트 (다음 봉 시가 체결, 부분 체결, 거부, 수수료, 스톱/타깃, 갭) | TESTED | `test_backtest` |
| 통계 검증 (IS/OOS, WF, look-ahead, 비용 스트레스, block bootstrap, Holm/BH, ablation, 국면, 민감도) | TESTED | `reports/backtest_report.html` |
| 생존 편향 검증 | PARTIAL | 상장폐지 종목 목록 API 없음. 유니버스가 사후 선택된 생존 종목이라는 한계만 명시 |
| 위험 관리 (거래당·일간 한도, 보유·미체결 위험 반영, 연속손실·DD 축소, 노출·자본 한도) | TESTED | `test_risk` |
| Kill Switch (영구 보존, sticky/transient, 수동 해제) | TESTED | `test_kill_switch`, PAPER 실행 |
| PaperBroker / PAPER 러너 | TESTED (2분 실행) | **4주 관찰 미실시** → Phase 4 완료 아님 |
| PAPER fleet (전략×시장 인스턴스 격리, 공용 WS, 재시작 복원) | TESTED | 3분 실행(9개 인스턴스) + Docker 컨테이너 실행 + `test_ops` |
| 텔레그램 알림 / 헬스체크 핑 / 워치독 자동 재시작 | IMPLEMENTED · Mock TESTED | 실제 텔레그램 발송은 운영자 Mac 에서 `notify-test` 로 확인 필요 |
| 수집기 무기한 실행 + 시간당 압축 + 디스크 경보 | TESTED | 3분 실행: 지난 날짜 563개 파일 → 15개 |
| 실시간 Binance 파생 피드 (B 전략 PAPER) | IMPLEMENTED · Mock TESTED · **BLOCKED here** | 한국 회선에서 `probe` 로 확인. 차단 시 B 인스턴스 자동 제외 |
| PAPER vs 백테스트 비교 리포트 (가동률·신호 일치·슬리피지·스프레드·공백) | TESTED | `paper-report`, `test_ops` |
| UpbitLiveBroker (중복 방지, UNKNOWN 처리, 재전송 금지, reconcile, 3중 게이트) | IMPLEMENTED · Mock TESTED | 실주문 없음. LIVE 비활성 |
| 대시보드 (시장·전략·자산·시스템) | TESTED | FastAPI 실행, 엔드포인트와 화면 확인 |
| 관리자 알림 | IMPLEMENTED | 텔레그램 (Kill Switch, 공백, 디스크, 멈춤, 일일 요약, 주간 리포트) |
| Docker Compose / 24h 무인 운영 | IMPLEMENTED | compose 파일 작성. 장시간 실행은 미검증 |

## 2. 프로젝트 구조

`README.md` 와 `docs/ARCHITECTURE.md` 참고. 소스 약 6,600줄, 테스트 약 1,100줄입니다.

## 3. 핵심 설계 결정

`docs/ARCHITECTURE.md` 에 정리했습니다. 요점: 하나의 결정 경로(TradingCore), 실행가격은 Upbit만, available_ts 정렬, 스윙 확정 지연, 다음 봉 체결, 차단 API 우회 금지, LIVE 3중 게이트.

## 4. 실제 API 연결 결과

`docs/API_ACCESS.md`. 요약: Upbit, Bithumb, OKX, Binance 아카이브는 OK / Binance fapi 451, Bybit 403 은 BLOCKED (측정 위치: 미국 클라우드).

## 5. 확보한 과거 데이터

| 데이터 | 범위 | 규모 |
|---|---|---|
| Upbit 1h / 15m / 1d (BTC, ETH, XRP) | 2022-01-01 ~ 2026-10-17 | 1h 약 41,9xx봉/종목, 결측 약 0.2% (점검 공백 22회) |
| Upbit KRW-USDT | 2024-06-07 ~ | 1h 20,647봉 |
| Upbit 체결 (REST 7일 + WS) | 2026-10-10 ~ 10-17 | BTC 1.08M, ETH 0.47M, XRP 0.78M 건 |
| Upbit 호가 스냅샷 (WS) | 2026-10-17 06:11–06:41 UTC | 26,882 건 |
| Binance futures/spot 1h klines, premium index | 2022-01 ~ 2026-10-15 | 약 41,9xx봉/종목 |
| Binance metrics (5분 OI, L/S, taker ratio) | 2022-01-01 ~ 2026-10-15 | 약 503k행/종목, 결측 133행 |
| Binance funding (확정) | 2022-01 ~ **2026-09-30** | 5,202건/종목 (10월분은 아직 미게시) |
| OKX OI 1h / funding 이력 | 최근 60일 / 96일 | 1,440 / 287건 |
| 청산 이벤트 | 실시간 30분 | OKX 4건 (Bybit·Binance 는 BLOCKED) |

## 6. 실행 명령

`README.md` "빠른 시작" 과 `scripts/phase1_2.sh` 참고.

## 7. 자동 테스트 결과

```
uv run pytest -m "not network"   → 91 passed
uv run pytest -m network         → 2 passed (Upbit 실 API, Binance 차단 감지)
uv run ruff check src tests      → All checks passed
```

테스트 과정에서 실제 결함을 찾아 고쳤습니다.

- 부분 체결된 청산의 실현손익이 거래 기록에서 빠지던 문제
- `.tmp-*.parquet` 파일이 glob 에 노출될 수 있던 문제
- Binance metrics 의 빈 필드("")가 아카이브 수집 전체를 중단시키던 문제
- 같은 밀리초 안의 체결이 ID 역순으로 오면 중복으로 버려질 수 있던 문제

## 8. 백테스트 HTML 리포트

`reports/backtest_report.html` (원자료 `reports/backtest_report.json`), `reports/data_quality.html`.
저장소에는 `reports/samples/` 에 사본을 커밋했습니다.

## 9. 전략별 순수익·MDD·Expectancy (1h, 2022-01 ~ 2026-10, 기본 비용 0.30%, 할당 300,000원)

| 전략 | BTC net / MDD / E[R] | ETH net / MDD / E[R] | XRP net / MDD / E[R] |
|---|---|---|---|
| Buy & Hold | +92.8% / 64.2% / – | −27.7% / 74.1% / – | +72.2% / 72.0% / – |
| BASE_ema_trend | **+98.5% / 16.6% / +0.49** | −6.6% / 21.2% / +0.27 | +24.9% / 20.0% / +0.51 |
| BASE_donchian | +13.2% / 21.7% / +0.13 | +28.6% / 16.9% / +0.19 | +43.1% / 31.6% / +0.64 |
| BASE_donchian + B3 필터 | +1.5% / 21.4% / +0.07 | +33.6% / 15.9% / +0.24 | +44.0% / 31.6% / +0.75 |
| A1 sweep reversal | −80.1% / 80.3% / −0.47 | −75.1% / 75.1% / −0.38 | −75.0% / 76.0% / −0.43 |
| A2 sweep + CHoCH | −25.7% / 27.1% / −0.20 | −6.8% / 16.6% / −0.04 | −23.2% / 24.7% / −0.20 |
| A3 sweep + FVG | −24.4% / 25.7% / −0.14 | −18.8% / 22.8% / −0.08 | −26.1% / 27.9% / −0.17 |
| B1 short squeeze | −13.4% / 14.5% / −0.20 | +2.4% / 13.5% / +0.00 | −2.1% / 10.4% / −0.01 |
| B2 liq reversal (청산확인 없음) | −2.8% / 7.0% / −0.05 | −1.9% / 8.9% / −0.03 | −3.1% / 7.1% / −0.06 |
| A+B: A2 AND derivConfirm +B3 | −22.9% / 25.0% / −0.19 | −4.7% / 17.7% / −0.05 | −21.9% / 23.6% / −0.19 |

**통계 판단**
- 블록 부트스트랩 기준 단독 p 값이 가장 낮은 것은 BASE_ema_trend/BTC 입니다 (Sharpe 1.04, 95% CI 0.05–1.99, p=0.017).
  하지만 42개 조합 동시 검정을 보정하면 Holm p=0.71, BH q=0.71 로 **유의한 전략은 없습니다**.
  무작위 진입 대비 p 도 0.088 입니다.
- Walk-forward (BTC, 12개월 학습 → 3개월 테스트, 16개 창):
  - BASE_donchian: OOS 누적 +18.4%, E[R] +0.35
  - A2: OOS −24.3%, E[R] −0.17
  - A3: OOS −12.3%, E[R] −0.08
- 기간별: 추세 기준전략은 개발 구간(2022–2024)에 이익이 집중돼 있습니다. 검증(2025)·테스트(2026-01~09)에서는 시장별로 엇갈립니다.
  예: BTC EMA 는 dev +94%, 2025 −2.7%, 2026 +5.0%. XRP Donchian 은 3개 구간 모두 +.
- 결합 효과(ablation): A2 에 파생 확인·B3 필터를 더하면 손실이 1–3%p 줄지만 음의 기대값은 그대로입니다.
  B3 필터는 Donchian 에서 ETH·XRP 는 개선, BTC 는 악화로 **일관성이 없습니다**. 단순한 쪽을 우선한다는 원칙에 따라 복합 전략을 채택하지 않습니다.
- 체결 기반 C 전략: 7일 표본에서 모두 음수입니다. 표본이 너무 작아 결론을 내릴 수 없습니다.
- 2025-10 급락 스트레스(10월 한 달): 전략 손익은 −4.0% ~ +1.9% 였습니다. 최악은 A1/ETH 입니다. 같은 기간 Buy & Hold XRP 는 −7.2% 였습니다.

## 10. 수수료·슬리피지 분석

- 기본 비용(왕복 0.30%)에서 비용 이전 수익(gross)의 상당 부분이 사라집니다.
  - A3/ETH: gross +23.1% → net −18.8% (비용 약 126k KRW)
  - BASE_donchian/ETH: 낙관 +58.5% / 기본 +28.6% / 스트레스 −1.2%
- A1 은 거래 1,100건 이상으로 회전율이 높아, gross 손실은 −4.5% 이지만 비용 때문에 −80% 가 됩니다.
- 슬리피지 가정(편도 = 왕복비용/2 − 수수료 0.05%)이 수수료보다 큽니다. 실제 Upbit 호가 스프레드(BTC 약 0.7bp, XRP 약 5.7bp, 실측 30분)와 체결 충격은 PAPER 운영에서 다시 측정해야 합니다.

## 11. 발견된 문제와 제약

1. **우위가 확인된 전략이 없습니다.** SMC 계열(A)은 세 시장·세 비용 시나리오·walk-forward 모두에서 음의 기대값입니다. 추세 기준전략은 개발 구간에 성과가 몰려 있고 다중검정을 통과하지 못합니다.
   → 현재 LIVE 전환 대상은 없습니다. 6개월 1억 원 목표는 어떤 결과로도 뒷받침되지 않습니다.
2. 지역 차단: Binance fapi, Bybit 는 이 개발 환경에서 BLOCKED 입니다. 한국 운영 서버에서 `cat probe` 로 다시 확인해야 합니다.
3. 데이터 공백
   - 과거 청산: 아카이브가 없어 B2 원설계를 검증할 수 없습니다.
   - 과거 호가: 없어서 C4 를 검증할 수 없습니다.
   - 체결: 7일분뿐이라 C 계열은 판단 불가입니다.
   - funding: Binance 아카이브는 월 단위 게시라 당월분이 없습니다.
4. 생존 편향: 유니버스(BTC, ETH, XRP)는 사후 선택입니다.
5. 운영 미완
   - PAPER 4주 관찰을 하지 않았습니다.
   - 관리자 알림 채널이 없습니다.
   - myOrder WS 가 없습니다.
   - 서버측 스톱이 없어서, 프로세스가 죽으면 손절이 실행되지 않습니다.
6. 이 저장소는 GitHub 원격이 아직 없습니다 (새 저장소 생성 권한 없음 → 운영자가 빈 저장소를 만들어야 push 가능).

## 12. 다음 단계 권장

1. 한국 운영 서버에서 `cat probe` 를 실행하고, Binance/Bybit 접근과 약관을 확인합니다.
2. collector 와 PAPER 를 Docker 로 24시간 운영합니다. **최소 4주 동안** 체결·호가·청산을 쌓고 안정성을 기록합니다 (Phase 4 완료 조건).
3. 쌓인 체결·호가로 C 계열과 B2(청산확인 버전)를 다시 평가합니다. 사전에 정한 파라미터를 쓰고 walk-forward 로만 평가합니다.
4. 추세 기준전략은 국면 필터 하나만 사전 가설로 정해 OOS 2026-10 이후 데이터로 검증합니다. 과최적화를 막기 위해 파라미터 탐색은 하지 않습니다.
5. 관리자 알림(Telegram 등), myOrder WebSocket, 외부 헬스체크를 추가합니다.
6. 위 조건을 통과한 전략이 생기면 `docs/LIVE_RUNBOOK.md` 절차에 따라 300,000원 승인 파일로 소액 LIVE 를 검토합니다.
