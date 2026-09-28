# 선택한 계좌·이체·카드·청구서 업무를 Python 함수로 구현합니다.
# 대상과 처리 조건을 검증하고 데이터를 조회하거나 변경합니다.
# 필요한 함수를 에이전트가 호출할 Tool로 제공합니다.

import calendar
import copy
import json
from datetime import date, datetime, timedelta, timezone
from typing import Literal

import pandas as pd
from langchain_core.tools import tool

from data_store import load_data

KST = timezone(timedelta(hours=9))
BASE_DATE = date(2026, 9, 20)


def get_base_date() -> date:
    return BASE_DATE


@tool(parse_docstring=True)
def get_account_balance_by_owner(owner_id: str, account_id: str | None = None) -> str:
    """계좌 잔액을 조회한다. 여러 계좌의 총액을 물으면 반환값의 total_balance를 그대로 쓰고 직접 더하지 않는다.

    Args:
        owner_id: 계좌 소유주의 아이디
        account_id: 조회할 계좌 번호. 생략하면 (None) 해당 owner_id의 모든 계좌를 조회한다.
    """
    data = load_data()

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
        ],
        'total_balance': sum(acc['balance'] for acc in matched),
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

    data = load_data()

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


def get_owner_accounts(data: dict, owner_id: str) -> list[dict]:
    return [a for a in data['accounts'] if a['owner_id'] == owner_id]


def get_transfer_time() -> datetime:
    return datetime.combine(get_base_date(), datetime.now(KST).time().replace(microsecond=0), tzinfo=KST)


def validate_transfer(data: dict, owner_id: str, from_account: str, to_account: str, amount: int) -> str | None:
    """이체할 수 없으면 그 이유를, 가능하면 None을 반환한다."""
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        return '이체 금액은 0보다 큰 원 단위 정수여야 합니다.'

    mine = {a['account_id']: a for a in get_owner_accounts(data, owner_id)}
    if from_account not in mine or to_account not in mine:
        return '이체할 수 없는 계좌가 포함되어 있습니다. 본인 소유의 계좌 사이에서만 이체할 수 있습니다.'
    if from_account == to_account:
        return '출금 계좌와 입금 계좌가 같습니다. 서로 다른 계좌를 지정해 주세요.'

    source = mine[from_account]
    if source['balance'] < amount:
        return f"{source['nickname']} 계좌의 잔액({source['balance']:,}원)이 이체 금액({amount:,}원)보다 적어 이체할 수 없습니다."
    return None


def compute_keep_balance_amount(data: dict, owner_id: str, from_account: str, keep_amount: int) -> int | None:
    """출금 계좌에 keep_amount를 남기고 이체할 금액을 계산한다. 계좌를 찾을 수 없으면 None."""
    mine = {a['account_id']: a for a in get_owner_accounts(data, owner_id)}
    if from_account not in mine:
        return None
    return mine[from_account]['balance'] - keep_amount


def validate_transfer_legs(data: dict, owner_id: str, from_account: str, legs: list[tuple[str, int]]) -> str | None:
    """하나의 출금 계좌에서 여러 (입금 계좌, 금액) legs로 나가는 분할 이체를 검증한다."""
    mine = {a['account_id']: a for a in get_owner_accounts(data, owner_id)}
    if from_account not in mine:
        return '이체할 수 없는 계좌가 포함되어 있습니다. 본인 소유의 계좌 사이에서만 이체할 수 있습니다.'

    total = 0
    for to_account, amount in legs:
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            return '이체 금액은 각각 0보다 큰 원 단위 정수여야 합니다.'
        if to_account not in mine:
            return '이체할 수 없는 계좌가 포함되어 있습니다. 본인 소유의 계좌 사이에서만 이체할 수 있습니다.'
        if to_account == from_account:
            return '출금 계좌와 입금 계좌가 같습니다. 서로 다른 계좌를 지정해 주세요.'
        total += amount

    source = mine[from_account]
    if source['balance'] < total:
        return f"{source['nickname']} 계좌의 잔액({source['balance']:,}원)이 이체 금액 합계({total:,}원)보다 적어 이체할 수 없습니다."
    return None


def build_split_preview(data: dict, owner_id: str, from_account: str, legs: list[tuple[str, int]]) -> dict:
    mine = {a['account_id']: a for a in get_owner_accounts(data, owner_id)}
    source = mine[from_account]
    total = sum(amount for _, amount in legs)
    leg_previews = [
        {
            'account_id': to_account,
            'nickname': mine[to_account]['nickname'],
            'balance_before': mine[to_account]['balance'],
            'balance_after': mine[to_account]['balance'] + amount,
            'amount': amount,
        }
        for to_account, amount in legs
    ]
    return {
        'from': {
            'account_id': from_account,
            'nickname': source['nickname'],
            'balance_before': source['balance'],
            'balance_after': source['balance'] - total,
        },
        'legs': leg_previews,
        'total': total,
    }


def apply_transfer_legs(data: dict, owner_id: str, from_account: str, legs: list[tuple[str, int]],
                         occurred_at: datetime) -> tuple[dict, list[dict]]:
    """원본을 바꾸지 않고, 여러 legs의 잔액·거래·처리 기록을 한 번에 반영한 새 데이터를 반환한다.
    legs 중 하나라도 저장할 수 없으면 예외를 던져 호출자가 원본을 그대로 유지하게 한다."""
    new_data = copy.deepcopy(data)
    accounts = {a['account_id']: a for a in new_data['accounts']}
    total = sum(amount for _, amount in legs)
    accounts[from_account]['balance'] -= total

    last_number = max((int(t['transaction_id'].split('-')[1]) for t in new_data['transactions']), default=0)
    stamp = occurred_at.isoformat(timespec='seconds')
    records = []
    offset = 0
    for to_account, amount in legs:
        accounts[to_account]['balance'] += amount

        offset += 1
        withdrawal_id = f'tx-{last_number + offset:03d}'
        offset += 1
        deposit_id = f'tx-{last_number + offset:03d}'
        request_id = f"req-{len(new_data['requests']) + 1:03d}"

        new_data['transactions'].append({
            'transaction_id': withdrawal_id, 'owner_id': owner_id, 'account_id': from_account,
            'type': 'withdrawal', 'amount': amount, 'occurred_at': stamp,
            'card_id': None, 'merchant': None, 'request_id': request_id,
        })
        new_data['transactions'].append({
            'transaction_id': deposit_id, 'owner_id': owner_id, 'account_id': to_account,
            'type': 'deposit', 'amount': amount, 'occurred_at': stamp,
            'card_id': None, 'merchant': None, 'request_id': request_id,
        })
        record = {
            'request_id': request_id, 'type': 'transfer', 'owner_id': owner_id,
            'from_account': from_account, 'to_account': to_account, 'amount': amount,
            'status': 'completed', 'created_at': stamp,
        }
        new_data['requests'].append(record)
        records.append(record)
    return new_data, records


def build_transfer_preview(data: dict, owner_id: str, from_account: str, to_account: str, amount: int) -> dict:
    mine = {a['account_id']: a for a in get_owner_accounts(data, owner_id)}
    source, target = mine[from_account], mine[to_account]
    return {
        'from': {
            'account_id': from_account,
            'nickname': source['nickname'],
            'balance_before': source['balance'],
            'balance_after': source['balance'] - amount,
        },
        'to': {
            'account_id': to_account,
            'nickname': target['nickname'],
            'balance_before': target['balance'],
            'balance_after': target['balance'] + amount,
        },
        'amount': amount,
    }


def apply_transfer(data: dict, owner_id: str, from_account: str, to_account: str,
                   amount: int, occurred_at: datetime) -> tuple[dict, dict]:
    """원본을 바꾸지 않고, 잔액·거래 2건·처리 기록이 모두 반영된 새 데이터를 반환한다."""
    new_data = copy.deepcopy(data)
    accounts = {a['account_id']: a for a in new_data['accounts']}
    accounts[from_account]['balance'] -= amount
    accounts[to_account]['balance'] += amount

    last_number = max((int(t['transaction_id'].split('-')[1]) for t in new_data['transactions']), default=0)
    request_id = f"req-{len(new_data['requests']) + 1:03d}"
    stamp = occurred_at.isoformat(timespec='seconds')

    for offset, (account_id, kind) in enumerate([(from_account, 'withdrawal'), (to_account, 'deposit')], start=1):
        new_data['transactions'].append({
            'transaction_id': f'tx-{last_number + offset:03d}',
            'owner_id': owner_id,
            'account_id': account_id,
            'type': kind,
            'amount': amount,
            'occurred_at': stamp,
            'card_id': None,
            'merchant': None,
            'request_id': request_id,
        })

    record = {
        'request_id': request_id,
        'type': 'transfer',
        'owner_id': owner_id,
        'from_account': from_account,
        'to_account': to_account,
        'amount': amount,
        'status': 'completed',
        'created_at': stamp,
    }
    new_data['requests'].append(record)
    return new_data, record


NICKNAME_MIN_LEN = 1
NICKNAME_MAX_LEN = 20


def validate_nickname_change(data: dict, owner_id: str, account_id: str, new_nickname: str) -> str | None:
    """별명을 바꿀 수 없으면 그 이유를, 가능하면 None을 반환한다."""
    mine = {a['account_id']: a for a in get_owner_accounts(data, owner_id)}
    if account_id not in mine:
        return '이체할 수 없는 계좌가 포함되어 있습니다. 본인 소유의 계좌만 별명을 바꿀 수 있습니다.'

    name = new_nickname.strip()
    if not (NICKNAME_MIN_LEN <= len(name) <= NICKNAME_MAX_LEN):
        return f'별명은 공백을 제외하고 {NICKNAME_MIN_LEN}자 이상 {NICKNAME_MAX_LEN}자 이하여야 합니다.'
    if name == mine[account_id]['nickname']:
        return '이미 같은 별명입니다.'
    if any(a['nickname'] == name for aid, a in mine.items() if aid != account_id):
        return f"'{name}'은(는) 이미 다른 계좌에서 쓰고 있는 별명입니다."
    return None


def build_nickname_preview(data: dict, owner_id: str, account_id: str, new_nickname: str) -> dict:
    mine = {a['account_id']: a for a in get_owner_accounts(data, owner_id)}
    account = mine[account_id]
    return {
        'account_id': account_id,
        'old_nickname': account['nickname'],
        'new_nickname': new_nickname.strip(),
        'balance': account['balance'],
    }


def apply_nickname_change(data: dict, owner_id: str, account_id: str, new_nickname: str) -> tuple[dict, dict]:
    """원본을 바꾸지 않고, 별명이 바뀐 새 데이터와 처리 기록을 반환한다."""
    new_data = copy.deepcopy(data)
    accounts = {a['account_id']: a for a in new_data['accounts']}
    old_nickname = accounts[account_id]['nickname']
    name = new_nickname.strip()
    accounts[account_id]['nickname'] = name

    request_id = f"req-{len(new_data['requests']) + 1:03d}"
    stamp = get_transfer_time().isoformat(timespec='seconds')
    record = {
        'request_id': request_id,
        'type': 'nickname_change',
        'owner_id': owner_id,
        'account_id': account_id,
        'old_nickname': old_nickname,
        'new_nickname': name,
        'status': 'completed',
        'created_at': stamp,
    }
    new_data['requests'].append(record)
    return new_data, record


# ---------- 카드 ----------

def get_owner_cards(data: dict, owner_id: str) -> list[dict]:
    return [c for c in data['cards'] if c['owner_id'] == owner_id]


def get_owner_addresses(data: dict, owner_id: str) -> list[dict]:
    return [a for a in data['addresses'] if a['owner_id'] == owner_id]


CARD_TYPE_LABELS = {'debit': '체크카드', 'credit': '신용카드'}
CARD_STATUS_LABELS = {'active': '사용 가능', 'locked': '일시 잠금', 'lost': '분실 정지'}


@tool(parse_docstring=True)
def get_cards_by_owner(owner_id: str, card_id: str | None = None) -> str:
    """카드 이름·ID·종류(신용·체크)·상태를 조회한다.

    Args:
        owner_id: 카드 소유주의 아이디
        card_id: 조회할 카드 번호. 생략하면 (None) 해당 owner_id의 모든 카드를 조회한다.
    """
    data = load_data()
    matched = [
        c for c in data['cards']
        if c['owner_id'] == owner_id and (card_id is None or c['card_id'] == card_id)
    ]
    if not matched:
        return json.dumps({'found': False, 'cards': []}, ensure_ascii=False)

    return json.dumps({
        'found': True,
        'cards': [
            {
                'card_id': c['card_id'],
                'name': c['name'],
                'card_type': CARD_TYPE_LABELS.get(c['card_type'], c['card_type']),
                'status': CARD_STATUS_LABELS.get(c['status'], c['status']),
            }
            for c in matched
        ],
    }, ensure_ascii=False)


def find_open_reissue(data: dict, owner_id: str, card_id: str) -> dict | None:
    """해당 카드의 취소되지 않은 재발급 신청을 찾는다(있으면 하나만 존재해야 한다)."""
    for r in data['reissue_applications']:
        if r['owner_id'] == owner_id and r['card_id'] == card_id and r['status'] != 'cancelled':
            return r
    return None


REISSUE_STATUS_LABELS = {'received': '접수', 'in_production': '제작 중', 'shipping': '배송 중', 'cancelled': '취소됨'}


@tool(parse_docstring=True)
def get_reissue_applications_by_owner(
    owner_id: str, card_id: str | None = None, reissue_id: str | None = None
) -> str:
    """카드 재발급 신청 내역에서 배송지와 처리 상태를 조회한다.

    Args:
        owner_id: 신청자의 아이디
        card_id: 조회할 카드 번호. 생략하면 해당 owner_id의 모든 신청을 조회한다.
        reissue_id: 조회할 신청 번호를 알고 있으면 지정한다. 생략 가능.
    """
    data = load_data()
    addresses = {a['address_id']: a for a in data['addresses']}
    matched = [
        r for r in data['reissue_applications']
        if r['owner_id'] == owner_id
        and (card_id is None or r['card_id'] == card_id)
        and (reissue_id is None or r['reissue_id'] == reissue_id)
    ]
    if not matched:
        return json.dumps({'found': False, 'applications': []}, ensure_ascii=False)

    return json.dumps({
        'found': True,
        'applications': [
            {
                'reissue_id': r['reissue_id'],
                'card_id': r['card_id'],
                'delivery_address': addresses.get(r['delivery_address_id'], {}).get('label'),
                'status': REISSUE_STATUS_LABELS.get(r['status'], r['status']),
                'created_at': r['created_at'],
            }
            for r in matched
        ],
    }, ensure_ascii=False)


CARD_STATUS_CHANGE_RESULT = {'lost': 'lost', 'lock': 'locked', 'unlock': 'active'}


def validate_card_status_change(data: dict, owner_id: str, card_id: str, kind: str) -> str | None:
    """카드 상태를 바꿀 수 없으면 그 이유를, 가능하면 None을 반환한다. kind는 'lost'/'lock'/'unlock'."""
    mine = {c['card_id']: c for c in get_owner_cards(data, owner_id)}
    if card_id not in mine:
        return '본인 소유의 카드만 처리할 수 있습니다.'

    status = mine[card_id]['status']
    if kind == 'lost':
        if status == 'lost':
            return '이미 분실 정지된 카드입니다.'
    elif kind == 'lock':
        if status != 'active':
            return f"{CARD_STATUS_LABELS.get(status, status)} 상태인 카드는 일시 잠금할 수 없습니다. 사용 가능한 카드만 잠글 수 있습니다."
    elif kind == 'unlock':
        if status != 'locked':
            return f"{CARD_STATUS_LABELS.get(status, status)} 상태인 카드는 잠금 해제할 수 없습니다. 일시 잠금된 카드만 해제할 수 있습니다."
    else:
        return '알 수 없는 처리입니다.'
    return None


def build_card_status_preview(data: dict, owner_id: str, card_id: str, kind: str) -> dict:
    mine = {c['card_id']: c for c in get_owner_cards(data, owner_id)}
    card = mine[card_id]
    new_status = CARD_STATUS_CHANGE_RESULT[kind]
    return {
        'card_id': card_id,
        'name': card['name'],
        'old_status': CARD_STATUS_LABELS.get(card['status'], card['status']),
        'new_status': CARD_STATUS_LABELS.get(new_status, new_status),
    }


def apply_card_status_change(data: dict, owner_id: str, card_id: str, kind: str) -> tuple[dict, dict]:
    """원본을 바꾸지 않고, 카드 상태가 바뀐 새 데이터와 처리 기록을 반환한다."""
    new_data = copy.deepcopy(data)
    cards = {c['card_id']: c for c in new_data['cards']}
    old_status = cards[card_id]['status']
    new_status = CARD_STATUS_CHANGE_RESULT[kind]
    cards[card_id]['status'] = new_status

    request_id = f"req-{len(new_data['requests']) + 1:03d}"
    stamp = get_transfer_time().isoformat(timespec='seconds')
    record = {
        'request_id': request_id,
        'type': 'card_status_change',
        'owner_id': owner_id,
        'card_id': card_id,
        'old_status': old_status,
        'new_status': new_status,
        'status': 'completed',
        'created_at': stamp,
    }
    new_data['requests'].append(record)
    return new_data, record


def validate_reissue_create(data: dict, owner_id: str, card_id: str, delivery_address_id: str) -> str | None:
    """재발급을 신청할 수 없으면 그 이유를, 가능하면 None을 반환한다."""
    mine_cards = {c['card_id']: c for c in get_owner_cards(data, owner_id)}
    if card_id not in mine_cards:
        return '본인 소유의 카드만 재발급을 신청할 수 있습니다.'
    if mine_cards[card_id]['status'] != 'lost':
        return '분실 정지된 카드만 재발급을 신청할 수 있습니다.'

    mine_addresses = {a['address_id'] for a in get_owner_addresses(data, owner_id)}
    if delivery_address_id not in mine_addresses:
        return '등록된 배송지 중에서만 선택할 수 있습니다.'

    existing = find_open_reissue(data, owner_id, card_id)
    if existing:
        return (
            f"이미 취소되지 않은 재발급 신청({existing['reissue_id']}, "
            f"{REISSUE_STATUS_LABELS.get(existing['status'], existing['status'])})이 있습니다."
        )
    return None


def build_reissue_create_preview(data: dict, owner_id: str, card_id: str, delivery_address_id: str) -> dict:
    mine_cards = {c['card_id']: c for c in get_owner_cards(data, owner_id)}
    addresses = {a['address_id']: a for a in data['addresses']}
    card = mine_cards[card_id]
    address = addresses[delivery_address_id]
    return {
        'card_id': card_id,
        'card_name': card['name'],
        'address_label': address['label'],
        'address': address['address'],
    }


def apply_reissue_create(data: dict, owner_id: str, card_id: str, delivery_address_id: str) -> tuple[dict, dict]:
    """원본을 바꾸지 않고, 재발급 신청이 추가된 새 데이터와 신청 기록을 반환한다."""
    new_data = copy.deepcopy(data)
    reissue_id = f"reissue-{len(new_data['reissue_applications']) + 1:03d}"
    stamp = get_transfer_time().isoformat(timespec='seconds')
    record = {
        'reissue_id': reissue_id,
        'card_id': card_id,
        'owner_id': owner_id,
        'delivery_address_id': delivery_address_id,
        'status': 'received',
        'created_at': stamp,
    }
    new_data['reissue_applications'].append(record)
    return new_data, record


def get_owner_reissue(data: dict, owner_id: str, reissue_id: str) -> dict | None:
    return next(
        (r for r in data['reissue_applications'] if r['owner_id'] == owner_id and r['reissue_id'] == reissue_id),
        None,
    )


def validate_reissue_edit(data: dict, owner_id: str, reissue_id: str, new_address_id: str) -> str | None:
    """재발급 신청의 배송지를 바꿀 수 없으면 그 이유를, 가능하면 None을 반환한다."""
    application = get_owner_reissue(data, owner_id, reissue_id)
    if application is None:
        return '본인 명의의 해당 재발급 신청을 찾을 수 없습니다.'
    if application['status'] != 'received':
        return f"이미 {REISSUE_STATUS_LABELS.get(application['status'], application['status'])} 상태라 배송지를 바꿀 수 없습니다. 제작이 시작되기 전까지만 변경할 수 있습니다."

    mine_addresses = {a['address_id'] for a in get_owner_addresses(data, owner_id)}
    if new_address_id not in mine_addresses:
        return '등록된 배송지 중에서만 선택할 수 있습니다.'
    if new_address_id == application['delivery_address_id']:
        return '이미 같은 배송지입니다.'
    return None


def validate_reissue_cancel(data: dict, owner_id: str, reissue_id: str) -> str | None:
    """재발급 신청을 취소할 수 없으면 그 이유를, 가능하면 None을 반환한다."""
    application = get_owner_reissue(data, owner_id, reissue_id)
    if application is None:
        return '본인 명의의 해당 재발급 신청을 찾을 수 없습니다.'
    if application['status'] != 'received':
        return f"이미 {REISSUE_STATUS_LABELS.get(application['status'], application['status'])} 상태라 취소할 수 없습니다. 제작이 시작되기 전까지만 취소할 수 있습니다."
    return None


def build_reissue_manage_preview(data: dict, owner_id: str, reissue_id: str, new_address_id: str | None = None) -> dict:
    application = get_owner_reissue(data, owner_id, reissue_id)
    addresses = {a['address_id']: a for a in data['addresses']}
    cards = {c['card_id']: c for c in data['cards']}
    preview = {
        'reissue_id': reissue_id,
        'card_name': cards[application['card_id']]['name'],
        'current_address': addresses[application['delivery_address_id']]['label'],
    }
    if new_address_id is not None:
        preview['new_address'] = addresses[new_address_id]['label']
    return preview


def apply_reissue_edit(data: dict, owner_id: str, reissue_id: str, new_address_id: str) -> tuple[dict, dict]:
    """원본을 바꾸지 않고, 배송지가 바뀐 새 데이터와 신청 기록을 반환한다."""
    new_data = copy.deepcopy(data)
    application = next(r for r in new_data['reissue_applications'] if r['reissue_id'] == reissue_id)
    application['delivery_address_id'] = new_address_id
    return new_data, application


def apply_reissue_cancel(data: dict, owner_id: str, reissue_id: str) -> tuple[dict, dict]:
    """원본을 바꾸지 않고, 신청이 취소 처리된 새 데이터와 신청 기록을 반환한다."""
    new_data = copy.deepcopy(data)
    application = next(r for r in new_data['reissue_applications'] if r['reissue_id'] == reissue_id)
    application['status'] = 'cancelled'
    return new_data, application
