# 선택한 계좌·이체·카드·청구서 업무를 Python 함수로 구현합니다.
# 대상과 처리 조건을 검증하고 데이터를 조회하거나 변경합니다.
# 필요한 함수를 에이전트가 호출할 Tool로 제공합니다.

import json
from langchain_core.tools import tool

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
