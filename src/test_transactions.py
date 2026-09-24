import json

from functions import get_account_transactions_by_owner as tool


def call(**kw):
    kw.setdefault('owner_id', 'user-001')
    return json.loads(tool.invoke(kw))


def check(name, cond, detail=''):
    assert cond, f'FAIL - {name} {detail}'
    print('PASS -', name, detail)


# 기간 계산 (기준일 2026-09-20, 일요일)
periods = {
    'today': ('2026-09-20', '2026-09-20'),
    'this_week': ('2026-09-14', '2026-09-20'),
    'last_week': ('2026-09-07', '2026-09-13'),
    'this_month': ('2026-09-01', '2026-09-30'),
    'last_month': ('2026-08-01', '2026-08-31'),
}
for p, (s, e) in periods.items():
    f = call(period=p)['applied_filters']
    check(f'period={p} -> {s}~{e}', (f['start_date'], f['end_date']) == (s, e))

# 소유권·정렬
all_r = call()
check('user-001 본인 계좌 거래만', all(t['account_id'] in ('acc-001', 'acc-002', 'acc-003') for t in all_r['transactions']), f'count={all_r["count"]}')
check('최근순 정렬', [t['occurred_at'] for t in all_r['transactions']] == sorted([t['occurred_at'] for t in all_r['transactions']], reverse=True))
check('user-002는 본인 계좌 거래만', all(t['account_id'] == 'acc-004' for t in call(owner_id='user-002')['transactions']))

# 출금 vs 카드 결제
check('acc-001 출금 10건', call(account_id='acc-001', transaction_type='withdrawal')['count'] == 10)
cards = call(account_id='acc-001', card_only=True)
check('acc-001 카드 결제 9건, 카드 이름 포함', cards['count'] == 9 and all(t['card_name'] for t in cards['transactions']))

# 금액 경계, 합계, 0건, 당일
m = call(account_id='acc-001', min_amount=50000, transaction_type='withdrawal')
check('50,000원 이상에 정확히 50,000 포함', any(t['amount'] == 50000 for t in m['transactions']))
check('합계 = 목록 합',
      all_r['total_withdrawal'] == sum(t['amount'] for t in all_r['transactions'] if t['type'] == 'withdrawal')
      and all_r['total_deposit'] == sum(t['amount'] for t in all_r['transactions'] if t['type'] == 'deposit'))
z = call(account_id='acc-002', transaction_type='withdrawal')
check('0건이면 적용 조건 반환', z['count'] == 0 and z['applied_filters']['account_id'] == 'acc-002')
check('기준일 당일(9/20) 2건', call(period='today')['count'] == 2)

# 오류 응답
errors = {
    'period+날짜 동시': dict(period='this_week', start_date='2026-09-01'),
    '시작>종료': dict(start_date='2026-09-30', end_date='2026-09-01'),
    '날짜 형식': dict(start_date='9월 1일'),
    'min>max': dict(min_amount=100, max_amount=10),
    '음수 금액': dict(min_amount=-1),
    '남의 계좌': dict(account_id='acc-004'),
    '없는 계좌': dict(account_id='acc-999'),
}
for name, kw in errors.items():
    r = call(**kw)
    check(f'오류: {name}', list(r.keys()) == ['error'], r.get('error', ''))
check('남의 계좌/없는 계좌 메시지 동일', call(account_id='acc-004') == call(account_id='acc-999'))

print('\n모든 테스트 통과')
