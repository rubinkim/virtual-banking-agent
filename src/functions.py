# 선택한 계좌·이체·카드·청구서 업무를 Python 함수로 구현합니다.
# 대상과 처리 조건을 검증하고 데이터를 조회하거나 변경합니다.
# 필요한 함수를 에이전트가 호출할 Tool로 제공합니다.

import calendar
import json
from datetime import date, timedelta
from typing import Literal

import pandas as pd
from langchain_core.tools import tool

DATA_PATH = 'data/data.json'
BASE_DATE = date(2026, 9, 20)


def get_base_date() -> date:
    return BASE_DATE


@tool(parse_docstring=True)
def get_account_balance_by_owner(owner_id: str, account_id: str | None = None) -> str:
    """계좌 잔액을 조회한다.
    
    Args:
        owner_id: 계좌 소유주의 아이디
        account_id: 조회할 계좌 번호. 생략하면 (None) 해당 owner_id의 모든 계좌를 조회한다.
    """
    file_path = 'data/data.json'

    with open(file_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    matched = [
        account for account in data['accounts']
        if account['owner_id'] == owner_id and (account_id is None or account['account_id'] == account_id)
    ]

    if not matched:
        return json.dumps({'found': False, 'accounts': []})

    return json.dumps({
        'found': True,
        'accounts': [
            {'account_id': acc['account_id'], 'nickname': acc['nickname'], 'balance': acc['balance']}
            for acc in matched
        ]
    })


def resolve_period(period: str, base: date) -> tuple[date, date]:
    if period == 'today':
        return base, base
    if period == 'this_week':
        start = base - timedelta(days=base.weekday())
        return start, start + timedelta(days=6)
    if period == 'last_week':
        start = base - timedelta(days=base.weekday() + 7)
        return start, start + timedelta(days=6)
    if period == 'this_month':
        first = base.replace(day=1)
    else:  # last_month
        first = (base.replace(day=1) - timedelta(days=1)).replace(day=1)
    last_day = calendar.monthrange(first.year, first.month)[1]
    return first, first.replace(day=last_day)


def build_transaction_view(data: dict) -> pd.DataFrame:
    transactions = pd.DataFrame(data['transactions'])
    accounts = pd.DataFrame(data['accounts'])[['account_id', 'nickname']]
    accounts = accounts.rename(columns={'nickname': 'account_nickname'})
    cards = pd.DataFrame(data['cards'])[['card_id', 'name', 'card_type']]
    cards = cards.rename(columns={'name': 'card_name'})

    view = transactions.merge(accounts, on='account_id', how='left')
    view = view.merge(cards, on='card_id', how='left')
    view['occurred_date'] = pd.to_datetime(view['occurred_at'], utc=False).apply(lambda t: t.date())
    return view


@tool(parse_docstring=True)
def get_account_transactions_by_owner(
    owner_id: str,
    account_id: str | None = None,
    period: Literal['this_month', 'last_month', 'this_week', 'last_week', 'today'] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    min_amount: int | None = None,
    max_amount: int | None = None,
    transaction_type: Literal['deposit', 'withdrawal'] | None = None,
    card_only: bool = False,
) -> str:
    """계좌의 거래내역(입금·출금)을 조건에 맞게 조회한다. 결과는 최근순이며 건수와 유형별 합계를 함께 반환한다.

    Args:
        owner_id: 계좌 소유주의 아이디
        account_id: 조회할 계좌 번호. 생략하면 해당 소유주의 모든 계좌를 대상으로 한다.
        period: 조회 기간 키워드. this_month(이번 달), last_month(지난달), this_week(이번 주, 월~일), last_week(지난주), today(오늘). start_date/end_date와 함께 쓸 수 없다. 생략하면 전체 기간.
        start_date: 조회 시작일(YYYY-MM-DD, 포함). period와 함께 쓸 수 없다.
        end_date: 조회 종료일(YYYY-MM-DD, 포함). period와 함께 쓸 수 없다.
        min_amount: 거래 금액 하한(원, 이상). 생략하면 제한 없음.
        max_amount: 거래 금액 상한(원, 이하). 생략하면 제한 없음.
        transaction_type: 거래 유형. deposit(입금) 또는 withdrawal(출금). 생략하면 둘 다.
        card_only: True면 카드를 사용해 결제한 거래만 조회한다. 사용자가 '결제'라고 표현하면 True로 지정한다(카드를 쓰지 않은 계좌 출금은 결제가 아니다). 사용한 카드 이름도 결과에 포함된다.
    """
    if period is not None and (start_date is not None or end_date is not None):
        return json.dumps({'error': 'period와 start_date/end_date는 함께 쓸 수 없습니다. 둘 중 하나만 지정해 주세요.'}, ensure_ascii=False)

    if period is not None:
        start, end = resolve_period(period, get_base_date())
    else:
        try:
            start = date.fromisoformat(start_date) if start_date else None
            end = date.fromisoformat(end_date) if end_date else None
        except ValueError:
            return json.dumps({'error': '날짜 형식이 올바르지 않습니다. YYYY-MM-DD 형식으로 지정해 주세요.'}, ensure_ascii=False)
        if start and end and start > end:
            return json.dumps({'error': f'시작일({start})이 종료일({end})보다 늦습니다. 기간을 다시 확인해 주세요.'}, ensure_ascii=False)

    if (min_amount is not None and min_amount < 0) or (max_amount is not None and max_amount < 0):
        return json.dumps({'error': '금액은 0 이상이어야 합니다.'}, ensure_ascii=False)
    if min_amount is not None and max_amount is not None and min_amount > max_amount:
        return json.dumps({'error': f'최소 금액({min_amount})이 최대 금액({max_amount})보다 큽니다. 금액 범위를 다시 확인해 주세요.'}, ensure_ascii=False)

    with open(DATA_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    if account_id is not None:
        owned = {a['account_id'] for a in data['accounts'] if a['owner_id'] == owner_id}
        if account_id not in owned:
            return json.dumps({'error': '조회할 수 없는 계좌입니다.'}, ensure_ascii=False)

    view = build_transaction_view(data)
    view = view[view['owner_id'] == owner_id]

    mask = pd.Series(True, index=view.index)
    if account_id is not None:
        mask &= view['account_id'] == account_id
    if start is not None:
        mask &= view['occurred_date'] >= start
    if end is not None:
        mask &= view['occurred_date'] <= end
    if min_amount is not None:
        mask &= view['amount'] >= min_amount
    if max_amount is not None:
        mask &= view['amount'] <= max_amount
    if transaction_type is not None:
        mask &= view['type'] == transaction_type
    if card_only:
        mask &= view['card_id'].notna()

    result = view[mask].sort_values('occurred_at', ascending=False)

    columns = ['transaction_id', 'occurred_at', 'type', 'amount', 'account_id',
               'account_nickname', 'merchant', 'card_id', 'card_name', 'card_type']
    rows = result[columns].astype(object).where(result[columns].notna(), None).to_dict('records')

    return json.dumps({
        'count': len(rows),
        'total_deposit': int(result.loc[result['type'] == 'deposit', 'amount'].sum()),
        'total_withdrawal': int(result.loc[result['type'] == 'withdrawal', 'amount'].sum()),
        'applied_filters': {
            'account_id': account_id,
            'start_date': start.isoformat() if start else None,
            'end_date': end.isoformat() if end else None,
            'min_amount': min_amount,
            'max_amount': max_amount,
            'transaction_type': transaction_type,
            'card_only': card_only,
        },
        'transactions': rows,
    }, ensure_ascii=False)
