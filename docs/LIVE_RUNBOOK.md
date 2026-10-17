# LIVE 전환 운영 절차 (소액 실거래)

**현재 상태: LIVE 비활성.** 코드에 LIVE 주문 기능은 구현돼 있고 Mock API로 테스트했지만, 실제 Upbit 주문은 한 건도 보낸 적이 없습니다.

## 1. 전제 조건 (모두 충족해야 함)

1. PAPER 모드를 **최소 4주** 24시간 운영합니다. 이 기간에 Kill Switch 오작동, 미해결 UNKNOWN 주문, 잔고 불일치가 없어야 합니다.
2. 실거래 후보 전략은 백테스트 리포트 기준으로 비용 차감 후 E[R] > 0, walk-forward OOS E[R] > 0, 다중검정 보정 후에도 유의해야 합니다.
   **2026-10-17 리포트 기준으로 이 기준을 통과한 전략은 없습니다** → 현재는 LIVE 전환 대상이 없습니다.
3. 같은 전략의 PAPER 성과가 백테스트 분포 안에 있어야 합니다 (체결 차이와 슬리피지 확인).

## 2. API 키

- Upbit Open API 키 권한: **자산조회, 주문조회, 주문하기**만 켭니다. **출금하기는 절대 켜지 않습니다.**
- 허용 IP를 운영 서버 IP로 제한합니다.
- 키는 `.env` 에만 둡니다 (git 제외). 테스트 코드는 실키를 쓰지 않습니다.
- 외부 보호자금 7,000,000원은 거래소에 입금하지 않습니다. 시스템에는 자동 입금 기능이 없습니다.

## 3. 3중 게이트 + 승인

LIVE 프로세스는 아래가 **모두** 참일 때만 시작합니다 (`LiveGuard.verify`).

1. 설정: `mode: LIVE` 이고 `live.enabled: true` (`config/live.yaml`, git 제외).
2. 환경변수: `CAT_ENABLE_LIVE=I_ACCEPT_REAL_MONEY_RISK`.
3. 서명된 승인 파일 `config/live_approval.json`. 허용 자금, 전략, 종목, 만료일을 담고 `CAT_LIVE_APPROVAL_SECRET` 으로 HMAC 서명합니다.

```bash
uv run python scripts/approve_live.py --by "운영자" --max-krw 300000 \
    --strategies A2_sweep_choch --markets KRW-BTC --days 14
CAT_CONFIG=config/live.yaml CAT_ENABLE_LIVE=I_ACCEPT_REAL_MONEY_RISK uv run cat live --strategy A2_sweep_choch
```

승인 범위를 벗어나는 주문(다른 종목, 다른 전략, 한도 초과, 만료 후)은 `PermissionError` 로 거부됩니다.

## 4. 시작 시 자동 점검

- `reconcile()`: DB의 미종결 주문을 identifier로 조회하고, 거래소 잔고와 내부 포지션을 대조합니다. 불일치하면 Kill Switch가 켜집니다(BALANCE_MISMATCH / ORDER_STATE_MISMATCH).
- 시계 동기화: 서버에 chrony/NTP가 필요합니다 (Docker 이미지에 chrony 포함). 시계 차이가 2초를 넘으면 `CLOCK_SKEW` 로 신규 주문을 차단합니다.

## 5. 알려진 위험 (수용하고 감시해야 함)

- **Upbit에는 서버측 스톱 주문이 없습니다.** 손절은 프로세스가 체결 가격을 보고 시장가로 매도하는 방식입니다. 프로세스·서버·네트워크가 멈추면 **손절이 실행되지 않습니다.**
  대응: 외부 헬스체크와 알림, 짧은 재시작 정책(`restart: unless-stopped`), 포지션 크기 제한.
- 손실 한도는 계획값입니다. 갭, 부분 체결, 거래소 장애가 있으면 1R보다 큰 손실이 날 수 있습니다 (백테스트 `test_stop_fills_at_stop_or_gap_open` 참고).
- 주문 결과가 불명확하면(타임아웃·5xx) 같은 주문을 **재전송하지 않습니다**. identifier로 조회해서 확인하고, 확인되지 않으면 신규 주문을 막은 채 운영자 확인을 기다립니다.
- Kill Switch가 켜져 있어도 **보호성 매도(손절·청산)는 허용**합니다. 신규 매수만 막습니다. 거래소에 연결할 수 없으면 매도 완료로 기록하지 않습니다(주문 상태는 거래소 응답으로만 갱신됨).

## 6. Kill Switch 조작

```bash
uv run cat kill-switch status --mode LIVE
uv run cat kill-switch on  --mode LIVE --reason "manual stop"
uv run cat kill-switch off --mode LIVE --operator "운영자" --reason "확인 완료"   # 해제는 수동으로만
```

## 7. 단계적 확대

300,000원으로 시작합니다. 최소 4주 동안 위험 한도 안에서 운영된 뒤에만 승인 파일을 새로 발급해 단계적으로 늘립니다
(최대 3,000,000원 = `max_trading_capital_krw`). 누적 손실 1,500,000원에서는 자동으로 영구 정지하며, 운영자가 직접 해제해야 합니다.
