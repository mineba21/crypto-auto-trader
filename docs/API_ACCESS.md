# API 접근 검증 결과 (2026-10-17)

`uv run cat probe` 로 측정했습니다. 원자료: `docs/generated/api_probe_results.json`.

> **측정 위치 주의**: 이 측정은 개발용 클라우드 컨테이너(미국 오하이오, Google Cloud)에서 했습니다.
> 대한민국 네트워크에서 측정한 결과가 아닙니다. 운영 서버(한국)에서 `uv run cat probe` 를 다시 실행해 이 표를 갱신해야 합니다.
> 지역 차단된 API에는 프록시·VPN 같은 우회 수단을 쓰지 않았고, 우회 기능도 만들지 않았습니다.

## 대한민국 회선 측정 (2026-10-18, 운영자 MacBook, `scripts/mac_start.sh` 5단계)

| 공급원 | 결과 | 비고 |
|---|---|---|
| Binance USD-M REST (`fapi`) | **OK** | B1 실시간 파생 피드 정상 (`collectors/binance/live_feed.py`) |
| Bybit V5 REST | **OK** | 청산 WS(`allLiquidation`) 수집 대상 |
| Binance `@forceOrder` WS | 연결은 됨 | 15초 안에 청산이 없어 메시지 없음 → probe 에 ERROR(timeout) 로 표시됐지만 차단은 아님 |

→ 운영(한국)에서는 Binance·Bybit 를 정상 소스로 씁니다. 아래 표는 개발용 미국 클라우드에서 측정한 원래 결과입니다.
수집기는 시작할 때마다 REST 로 접근을 다시 확인하고, 차단된 소스는 건너뜁니다(우회 없음).

## 개발 클라우드 측정 (2026-10-17, 미국)

| 공급원 | 경로 | 결과(이 컨테이너) | 비고 |
|---|---|---|---|
| Upbit REST quotation | `api.upbit.com/v1/*` | **OK** (≈220 ms) | `Remaining-Req: group=…; min=600; sec=9` → 그룹별 초당 약 10회 |
| Upbit WebSocket | `wss://api.upbit.com/websocket/v1` | **OK** | trade, orderbook 실시간 수신 확인 |
| Upbit 체결 REST | `/v1/trades/ticks` | **OK** | `days_ago` 1–7: **최근 7일까지만** 제공. `cursor=sequential_id` 로 페이지 이동 |
| Upbit 호가 단위 | `/v1/orderbook/instruments` | OK | `tick_size` 제공 (2025-07-31 호가 정책 변경 반영) |
| Upbit 주문 API | `POST /v1/orders`, `/v1/orders/test` | 미호출 (키 없음) | 문서 확인: JSON body, `identifier` 64자·재사용 불가, 주문 그룹 초당 12회, test 그룹 초당 8회. **서버측 스톱 주문 없음** |
| Bithumb REST | `api.bithumb.com/v1/*` | **OK** | Upbit 호환 스키마. 공개 데이터만 구현 (실거래 = NOT IMPLEMENTED) |
| Binance USD-M REST | `fapi.binance.com` | **BLOCKED (HTTP 451)** | "restricted location" (이 컨테이너의 지역 기준) |
| Binance futures WS | `fstream.binance.com` | 판단 불가 | 15초 안에 메시지 없음(타임아웃). REST가 차단된 지역이라 더 시도하지 않음 |
| Binance 공식 아카이브 | `data.binance.vision` | **OK** | 과거 klines, metrics(OI), fundingRate, premiumIndexKlines. **liquidationSnapshot 은 404 (미제공)** |
| Bybit V5 REST | `api.bybit.com` | **BLOCKED (HTTP 403)** | CloudFront 국가 차단. `stream.bybit.com` WS는 열렸지만, REST의 차단 의도를 존중해 이 지역에서는 수집하지 않음 (한국에서는 OK) |
| OKX V5 REST | `www.okx.com/api/v5/*` | **OK** | OI, funding 이력, rubik OI 이력, 현물 시세 |
| OKX WS | `wss://ws.okx.com/ws/v5/public` | **OK** (443 포트) | `liquidation-orders` 실시간 수신. `:8443` 포트는 연결이 끊김(reset) |

## 키 없이 쓸 수 있는 범위

- Upbit: 마켓 목록, 캔들(초/분/일), 최근 7일 체결, 호가, 현재가, WS 체결·호가. 잔고·주문은 키가 필요합니다.
- Binance 아카이브: 2021-12 이후 5분 OI 메트릭, 2022 이전부터의 funding, klines, premium index.
- OKX: 현재 OI·추정 funding·mark price, funding 이력(약 3개월), OI 이력(rubik, 짧은 최근 구간만).

## 과거 데이터 제공 범위와 확인된 포맷 변경

- `metrics`(OI, long/short ratio): BTCUSDT 기준 **2021-12-01 시작**, 5분 간격. `create_time` 은 UTC 문자열입니다.
  소수점 자릿수가 기간마다 다르고(2021-11 vs 2026), 늦게 시작하는 날도 있습니다(예: 2026-10-15 01:15 시작).
  → 파서는 이름으로 컬럼을 고정하고, 공백은 품질 리포트에 기록하며, 값을 채워 넣지 않습니다.
- `spot/monthly/klines`: **헤더 없음**. **2025-01부터 타임스탬프가 마이크로초**로 바뀌었습니다 → ms로 정규화합니다.
- `futures/um/*/klines`: 헤더 있음, ms 단위입니다.
- `fundingRate`: 확정(settled) 값만 있습니다. 실시간 추정치는 별도 시리즈(`funding/okx_estimate`, `is_settled=False`)로 저장합니다.
- 강제청산: 공개 과거 아카이브가 없습니다. 실시간 WS로 수집한 이벤트만 존재하며, 거래소가 이벤트를 집계·제한할 수 있으므로 **하한값(lower bound)** 으로 취급합니다.
  - Binance `!forceOrder@arr`: 심볼당 1000ms 안의 **마지막 1건만** 전송됩니다. `S=SELL` 은 롱 강제청산입니다.
  - Bybit `allLiquidation.{symbol}`: 500ms 주기입니다. 문서상 **`S=Buy` 가 롱 포지션 청산**이고, `p` 는 파산가격입니다.
  - OKX `liquidation-orders`: `posSide=long` 이 롱 청산입니다. 수량은 계약 수라서 `ctVal` 을 곱해 명목가를 구합니다.

## 재연결 시 누락 복구

- Upbit 체결: 재연결 후 REST `trades/ticks` 로 마지막 `sequential_id` 이후분을 다시 재생합니다. REST 구간이 마지막 WS 체결까지 닿지 않으면 `gaps` 레코드를 남기고, 그 구간의 CVD는 무효(valid=False)로 처리합니다.
- Upbit 호가: 과거 스냅샷 API가 없어 복구할 수 없습니다. 재연결 구간은 항상 gap으로 기록하고 OBI를 무효화합니다.

## 약관·지역 제한

- Binance·Bybit 는 이 컨테이너 지역(미국)을 서비스 제한 지역으로 응답했습니다. 한국에서 접근할 수 있는지와 약관상 허용되는지는 운영자가 직접 확인해야 합니다.
- 해외 선물 거래소는 **읽기 전용 데이터 공급원**으로만 씁니다. 코드에 해외 주문 기능은 없습니다.
- 적법한 대체 공급원(구현됨): Binance 공식 아카이브(과거), OKX 공개 API(실시간·최근).
  추가 후보: Coinglass·Coinalyze 등 유료 집계 API(약관 확인 필요, 미구현).
