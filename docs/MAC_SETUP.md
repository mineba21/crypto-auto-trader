# Mac 에서 4주 PAPER 운영하기

실주문은 나가지 않습니다. Upbit API 키는 필요 없습니다. Docker 안에서 서비스 3개가 돌아갑니다.

| 서비스 | 하는 일 |
|---|---|
| `collector` | Upbit 체결·호가(WS), OKX OI·펀딩·청산을 무기한 수집. 1시간마다 지난 날짜의 작은 파일을 합칩니다. |
| `paper` | 9개 모의매매 인스턴스(A2·EMA·Donchian × BTC·ETH·XRP)와 B1(Binance 접근 시)을 각각 가상 30만원으로 운영 |
| `dashboard` | http://127.0.0.1:8501 (이 Mac 에서만 열림) |

## 빠른 시작 (명령 하나)

Docker Desktop 설치 후 터미널에서 실행합니다.
```bash
cd ~/Desktop/claude && git clone https://github.com/mineba21/crypto-auto-trader.git && cd crypto-auto-trader
scripts/mac_start.sh
```
스크립트가 아래 순서로 처리합니다.
1. Docker Desktop 을 확인합니다.
2. 텔레그램 키를 가져옵니다 (기본 경로 `/Users/soohan/Desktop/claude/test1/myStockApp-scanner-vm/.env`).
3. 전원 연결 시 잠자기를 끕니다 (비밀번호 1회).
4. 이미지를 빌드합니다.
5. 텔레그램 테스트 메시지를 보내고 거래소 접근을 확인합니다.
6. Binance 가 열리면 B1 용 데이터를 받습니다.
7. 서비스를 시작합니다.

다시 실행해도 안전합니다(수집 데이터와 모의 계좌 상태는 유지). 아래는 각 단계의 상세 설명입니다.

## 1. 준비 (한 번만)

1. **Docker Desktop for Mac** 을 설치합니다 (Apple Silicon / Intel 중 맞는 버전).
   - Settings → General → **Start Docker Desktop when you sign in to your computer** 를 켭니다.
   - Settings → Resources 는 기본값(메모리 4GB 이상)이면 충분합니다.
2. 코드를 받습니다.
   ```bash
   cd ~/Desktop/claude
   git clone https://github.com/mineba21/crypto-auto-trader.git
   cd crypto-auto-trader
   ```
3. 텔레그램 설정을 기존 프로젝트에서 가져옵니다. 텔레그램 관련 키만 복사하고, 값은 화면에 출력하지 않습니다.
   ```bash
   scripts/import_telegram_env.sh /Users/soohan/Desktop/claude/test1/myStockApp-scanner-vm/.env
   ```
   키 이름이 달라서 `imported: 0` 이 나오면 `.env` 를 열어 `TELEGRAM_BOT_TOKEN=`, `TELEGRAM_CHAT_ID=` 를 직접 채웁니다.
4. **healthchecks.io** 에서 Mac 이 꺼지거나 잠들었을 때 받을 알림을 설정합니다 (무료).
   1. 가입 후 Check 를 2개 만듭니다: `collector`, `paper`. Period 5분, Grace 15분.
   2. Integrations 에서 Telegram 을 연결합니다.
   3. 각 Check 의 ping URL 을 `.env` 에 넣습니다.
      ```
      HEALTHCHECK_URL_COLLECTOR=https://hc-ping.com/....
      HEALTHCHECK_URL_PAPER=https://hc-ping.com/....
      ```
5. **잠자기를 끕니다.** 가장 흔한 데이터 공백 원인입니다.
   - System Settings → Battery(또는 Energy) → 전원 어댑터 사용 시 **"디스플레이가 꺼져 있을 때 자동으로 잠자기 방지"** 를 켭니다.
   - 터미널에서 다음을 실행하면 전원 연결 중에는 잠들지 않습니다.
     ```bash
     sudo pmset -c sleep 0 disksleep 0
     ```
   - MacBook 은 **덮개를 닫으면 잠듭니다.** 외부 모니터와 전원이 연결된 클램셸 모드가 아니라면 덮개를 열어 두세요.
   - 자동 업데이트로 재부팅돼도 괜찮습니다. 로그인하면 Docker 가 다시 올라오고, 서비스가 자동 재시작되며 상태를 복원합니다.

## 2. 시작 전 점검

```bash
docker compose -f docker/compose.yaml build
docker compose -f docker/compose.yaml run --rm paper notify-test   # 텔레그램 테스트 메시지 + 헬스체크 핑
docker compose -f docker/compose.yaml run --rm paper probe         # 한국 회선에서 Binance/Bybit 접근 여부
```

- `probe` 에서 Binance `fapi` 가 OK 면 B1 인스턴스도 같이 돌아갑니다. BLOCKED 면 자동으로 빠지고 텔레그램으로 알립니다.
- B1 을 쓰려면 파생 데이터 이력이 필요합니다. 아래로 Binance 아카이브를 최근 4개월치만 받아 둡니다 (몇 분 걸림).
  ```bash
  docker compose -f docker/compose.yaml run --rm paper collect-history --sources binance_archive --start 2026-06-01
  ```

## 3. 시작 / 확인 / 정지

```bash
docker compose -f docker/compose.yaml up -d          # 시작 (백그라운드)
docker compose -f docker/compose.yaml ps             # 상태
docker compose -f docker/compose.yaml logs -f paper  # 로그 보기 (Ctrl-C 로 빠져나옴)
open http://127.0.0.1:8501                           # 대시보드 (인스턴스별 전환 가능)
docker compose -f docker/compose.yaml down           # 정지 (상태는 볼륨에 남음)
```

긴급 정지 (모든 모의 인스턴스 신규 진입 차단):
```bash
docker compose -f docker/compose.yaml exec paper uv run --no-sync cat kill-switch on --mode PAPER --reason "manual"
docker compose -f docker/compose.yaml exec paper uv run --no-sync cat kill-switch off --mode PAPER --operator soohan
```

## 4. 받게 되는 알림 (텔레그램)

- 시작 / 재시작 (`▶️`)
- Kill Switch 발동 (`🛑` 고정 사유, `⚠️` 데이터 지연 같은 일시 사유는 1시간에 1번)
- 데이터 공백, 디스크 부족 (여유 10GB 미만 경고, 3GB 미만 심각)
- 프로세스 멈춤 → 자동 재시작 (`🛑 main loop stalled`)
- **매일 09:00 KST 요약**: 인스턴스별 손익·포지션·체결 수, 수집량, 재연결 수, 디스크
- **매주 월요일 09:10 KST**: 주간 비교 리포트. 파일은 `reports/paper_report_YYYYMMDD.html` 에 생기고 Mac 에서 바로 열 수 있습니다.
- Mac 이 꺼지거나 잠들면: healthchecks.io 가 15분 뒤 알림

리포트를 직접 만들려면:
```bash
docker compose -f docker/compose.yaml exec paper uv run --no-sync cat paper-report --days 7
```

## 5. 4주 뒤 합격 기준 (`paper-report --days 28`)

| 기준 | 목표 |
|---|---|
| 평균 가동률 | ≥ 99% (4주 동안 꺼진 시간 합계 약 6.7시간 이하) |
| 신호 일치 | 모든 인스턴스에서 모의매매와 백테스트의 판단이 100% 일치 |
| 실측 슬리피지 | 백테스트 가정치(편도 10bps) 이하 |
| Kill Switch | 원인을 설명할 수 없는 발동 0건 |

모두 통과해도 **곧바로 실거래하지 않습니다.** 백테스트에서 통계적으로 우위가 확인된 전략이 아직 없기 때문입니다 (`docs/PHASE_REPORT.md`).
이번 4주는 시스템 안정성 검증과 체결·호가·청산 데이터 축적이 목적입니다.

## 6. 용량

- 4주 예상 사용량은 약 5GB 내외(추정)이고 대부분 호가 스냅샷입니다. 시장 데이터는 Mac 의 `crypto-auto-trader/data/` 에 쌓입니다.
- 운영 DB(SQLite)는 Docker 볼륨 `ops` 에 있습니다. 백업은 다음과 같이 합니다.
  ```bash
  docker run --rm -v docker_ops:/ops -v "$PWD":/out alpine tar czf /out/ops_backup.tgz -C /ops .
  ```
