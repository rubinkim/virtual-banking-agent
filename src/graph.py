# 업무 흐름에 필요한 State와 노드 함수를 정의합니다.
# 요청 해석, 업무 함수 호출, 승인·거절과 결과 안내를 연결합니다.
# LangGraph의 중단·재개로 사용자 승인을 처리합니다.

import os
from dotenv import load_dotenv

from datetime import datetime
from typing import Annotated, TypedDict, Literal
from pydantic import BaseModel, Field, ValidationError

from langgraph.graph.message import add_messages
from langchain_core.messages import AIMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.types import interrupt

from data_store import load_data, save_data
from functions import (
    get_account_balance_by_owner,
    get_account_transactions_by_owner,
    get_base_date,
    get_owner_accounts,
    get_transfer_time,
    validate_transfer,
    build_transfer_preview,
    apply_transfer,
)

load_dotenv()
GEMINI_MODEL = os.getenv('GEMINI_MODEL')

account_tools = [get_account_balance_by_owner, get_account_transactions_by_owner]

llm = ChatGoogleGenerativeAI(model=GEMINI_MODEL)
llm_with_tools = llm.bind_tools(account_tools)


class TransferAccounts(BaseModel):
    from_account: str = Field(description="이체로 자금이 빠져나가는 account_id")
    to_account: str = Field(description="이체로 자금이 들어가는 account_id")
    amount: int = Field(gt=0, description="이체 금액(원 단위 정수)")
    date: datetime | None = Field(default=None, description="이체 시각. 실행 시점에 코드가 채운다")


class BankState(TypedDict):
    messages: Annotated[list, add_messages]
    owner_id: str
    next: str
    transfer: TransferAccounts | None


class RouterDecision(BaseModel):
    next: Literal["account_agent", "transfer_agent"]

router_llm = llm.with_structured_output(RouterDecision)


def supervisor(state: BankState) -> BankState:
    """사용자 요청을 분석해서 어떤 subagent로 보낼지 결정하는 노드"""

    messages = state['messages']
    system = SystemMessage(
        content=(
            "당신은 은행 업무 요청을 분석해서 적절한 담당 agent로 routing하는 supervisor 입니다. "
            "담당 agent는 두 가지입니다. "
            "account_agent: 계좌 목록·잔액 조회, 거래내역 조회. "
            "transfer_agent: 사용자 본인의 계좌 사이의 이체. 이체 정보(출금 계좌, 입금 계좌, 금액)를 되물은 직후 사용자가 그에 답하는 경우도 transfer_agent입니다. "
            "그 밖의 요청도 가장 가까운 agent의 이름을 그대로 반환하세요. "
        )        
    )
    decision = router_llm.invoke([system, *messages])
    return {'next': decision.next}


def route_from_supervisor(state: BankState) -> str:
    return state['next']


def account_call_model(state: BankState) -> BankState:
    """사용자 요청을 보고 tool을 호출할 지 안할 지 그리고 만약 호출한다면 어떤 tool을 호출할 지 판단하는 노드"""

    owner_id = state['owner_id']
    messages = state['messages']
    system = SystemMessage(
        content=(
            f"당신은 accounts domain(은행계좌잔액조회, 은행거래내역조회)안의 업무를 처리하는 agent입니다. 이체는 별도의 담당 agent가 처리합니다. "
            f"현재 login한 사용자의 owner_id는 '{owner_id}'입니다. "
            f"tool을 호출할 때 owner_id가 필요하다면 이 값을 owner_id 인자로 그대로 사용하세요. "
            f"owner_id 같은 내부 식별자는 답변에 언급하지 마세요. "
            f"account_id를 모르면 생략하고 호출해 전체 계좌를 조회한 뒤, 그 결과(nickname 포함)를 보고 답변을 구성하세요. "
            f"사용자가 특정 계좌를 지목하지 않았다면 조회된 계좌 전부를 나열해서 답하세요. "
            f"사용자가 지목한 이름과 일치하는 계좌가 2개 이상이면, 실행하지 말고 어떤 계좌인지 사용자에게 되물으세요. "
            f"거래내역을 조회할 때 특정 계좌(별명)가 지정되었다면, account_id를 모르는 경우 먼저 잔액 조회 tool로 계좌 목록을 확인해 account_id를 알아낸 뒤 거래내역 tool을 호출하세요. "
            f"기간 표현(이번 달, 지난주 등)은 날짜를 직접 계산하지 말고 period 키워드로 전달하세요. "
            f"오늘(기준일)은 {get_base_date().isoformat()}입니다. start_date/end_date를 지정할 때 연도가 없는 날짜(예: 9월 1일)는 기준일의 연도로 해석하세요. "
            f"'결제'는 카드를 사용해 지출한 거래만을 뜻하므로 card_only=True로 조회하고, '출금'은 카드 결제와 계좌 출금을 모두 포함하니 구분해서 안내하세요. "
            f"아직 지원하지 않는 기능에 대한 요청이면, 추측해서 답하지 말고 현재 처리할 수 없다고 안내하세요."
        )
    )
    response = llm_with_tools.invoke([system, *messages])
    return {'messages': [response]}


def account_enforce_owner(state: BankState) -> BankState:
    """LLM이 만든 tool_calls의 owner_id를 State의 값으로 강제 교체하는 노드"""

    last_message = state['messages'][-1]
    safe_calls = [
        {**call, 'args': {**call['args'], 'owner_id': state['owner_id']}}
        for call in last_message.tool_calls
    ]
    return {'messages': [last_message.model_copy(update={'tool_calls': safe_calls})]}


account_builder = StateGraph(BankState)
account_builder.add_node("account_call_model", account_call_model)
account_builder.add_node("tools", ToolNode(account_tools))
account_builder.add_node("account_enforce_owner", account_enforce_owner)

account_builder.add_edge(START, "account_call_model")
account_builder.add_conditional_edges(
    "account_call_model", tools_condition, {"tools": "account_enforce_owner", END: END}
)
account_builder.add_edge("account_enforce_owner", "tools")
account_builder.add_edge("tools", "account_call_model")  # tool 결과를 다시 모델에게 넘겨 답변을 생성하도록 설계한다.
account_graph = account_builder.compile()


class TransferDraft(BaseModel):
    from_account: str | None = Field(default=None, description="돈이 빠져나가는 계좌의 account_id. 밝히지 않았거나 특정할 수 없으면 null")
    to_account: str | None = Field(default=None, description="돈이 들어가는 계좌의 account_id. 밝히지 않았거나 특정할 수 없으면 null")
    amount: int | None = Field(default=None, description="이체 금액(원 단위 정수). 예: 50만 원은 500000. 밝히지 않았으면 null")


transfer_extract_llm = llm.with_structured_output(TransferDraft)

TRANSFER_FIELD_LABELS = {'from_account': '출금 계좌', 'to_account': '입금 계좌', 'amount': '이체 금액'}


def transfer_extract(state: BankState) -> BankState:
    """대화에서 이체 정보(출금 계좌, 입금 계좌, 금액)를 뽑아 State.transfer를 채우는 노드"""

    accounts = get_owner_accounts(load_data(), state['owner_id'])
    account_lines = '\n'.join(f"- {a['account_id']}: {a['nickname']}" for a in accounts)
    system = SystemMessage(
        content=(
            "당신은 은행 이체 요청에서 이체 정보를 추출합니다. "
            "대화에서 가장 최근의 이체 요청 하나만 추출하고, 이미 완료되었거나 취소된 이전 이체는 무시하세요. "
            "정보가 부족해 되물은 적이 있다면 사용자의 이번 답변을 합쳐서 판단하세요. "
            f"사용자의 계좌 목록은 다음과 같습니다.\n{account_lines}\n"
            "사용자가 별명으로 말하면 위 목록에서 해당 계좌의 account_id로 바꾸세요. "
            "사용자가 account_id(예: acc-004)를 직접 말했다면 목록에 없더라도 그대로 반환하세요. "
            "별명이 어느 계좌인지 특정할 수 없거나 말하지 않은 값은 추측하지 말고 null로 두세요."
        )
    )
    draft = transfer_extract_llm.invoke([system, *state['messages']]) or TransferDraft()

    missing = [label for key, label in TRANSFER_FIELD_LABELS.items() if getattr(draft, key) is None]
    if missing:
        names = ', '.join(a['nickname'] for a in accounts)
        text = f"이체를 진행하려면 {', '.join(missing)} 정보가 더 필요합니다. (보유 계좌: {names}) 다시 말씀해 주세요."
        return {'messages': [AIMessage(content=text)], 'transfer': None}

    try:
        transfer = TransferAccounts(from_account=draft.from_account, to_account=draft.to_account, amount=draft.amount)
    except ValidationError:
        text = '이체 금액은 0보다 큰 원 단위 정수여야 합니다. 금액을 다시 말씀해 주세요.'
        return {'messages': [AIMessage(content=text)], 'transfer': None}
    return {'transfer': transfer}


def transfer_validate(state: BankState) -> BankState:
    """이체할 수 있는 요청인지 검증하는 노드. 실패하면 이유를 안내하고 State.transfer를 비운다"""

    transfer = state['transfer']
    reason = validate_transfer(load_data(), state['owner_id'], transfer.from_account, transfer.to_account, transfer.amount)
    if reason:
        return {'messages': [AIMessage(content=f'이체를 진행할 수 없습니다. {reason}')], 'transfer': None}
    return {}


def transfer_approve(state: BankState) -> BankState:
    """변경 내용을 보여주고 interrupt로 승인·거절을 받는 노드. 거절하면 변경 없이 State.transfer를 비운다"""

    transfer = state['transfer']
    preview = build_transfer_preview(load_data(), state['owner_id'], transfer.from_account, transfer.to_account, transfer.amount)
    source, target = preview['from'], preview['to']
    message = (
        '다음 이체를 진행할까요?\n'
        f"- 출금: {source['nickname']}({source['account_id']}) {source['balance_before']:,}원 → {source['balance_after']:,}원\n"
        f"- 입금: {target['nickname']}({target['account_id']}) {target['balance_before']:,}원 → {target['balance_after']:,}원\n"
        f"- 이체 금액: {preview['amount']:,}원"
    )
    decision = interrupt({'message': message, 'preview': preview})

    if decision == 'approve':
        return {}
    return {'messages': [AIMessage(content='이체를 취소했습니다. 잔액과 거래내역은 변경되지 않았습니다.')], 'transfer': None}


def transfer_execute(state: BankState) -> BankState:
    """승인된 이체를 다시 검증한 뒤 잔액·거래내역·처리 기록을 한 번에 저장하는 노드"""

    transfer = state['transfer']
    owner_id = state['owner_id']
    data = load_data()

    reason = validate_transfer(data, owner_id, transfer.from_account, transfer.to_account, transfer.amount)
    if reason:
        text = f'승인 이후 다시 확인해 보니 이체를 진행할 수 없습니다. {reason}'
        return {'messages': [AIMessage(content=text)], 'transfer': None}

    completed = transfer.model_copy(update={'date': get_transfer_time()})
    try:
        new_data, record = apply_transfer(data, owner_id, completed.from_account, completed.to_account, completed.amount, completed.date)
        save_data(new_data)
    except Exception:
        text = '저장 중 오류가 발생해 이체가 반영되지 않았습니다. 잔액과 거래내역은 그대로입니다.'
        return {'messages': [AIMessage(content=text)], 'transfer': None}

    balances = {a['account_id']: a for a in new_data['accounts']}
    source, target = balances[completed.from_account], balances[completed.to_account]
    text = (
        '이체가 완료되었습니다.\n'
        f"- {source['nickname']} → {target['nickname']}: {completed.amount:,}원\n"
        f"- 이체 후 잔액: {source['nickname']} {source['balance']:,}원, {target['nickname']} {target['balance']:,}원\n"
        f"- 처리 번호: {record['request_id']}"
    )
    return {'messages': [AIMessage(content=text)], 'transfer': None}


def transfer_continues(state: BankState) -> str:
    return 'continue' if state.get('transfer') is not None else END


def build_transfer_graph(extract=transfer_extract):
    builder = StateGraph(BankState)
    builder.add_node("transfer_extract", extract)
    builder.add_node("transfer_validate", transfer_validate)
    builder.add_node("transfer_approve", transfer_approve)
    builder.add_node("transfer_execute", transfer_execute)

    builder.add_edge(START, "transfer_extract")
    builder.add_conditional_edges("transfer_extract", transfer_continues, {"continue": "transfer_validate", END: END})
    builder.add_conditional_edges("transfer_validate", transfer_continues, {"continue": "transfer_approve", END: END})
    builder.add_conditional_edges("transfer_approve", transfer_continues, {"continue": "transfer_execute", END: END})
    builder.add_edge("transfer_execute", END)
    return builder.compile()


transfer_graph = build_transfer_graph()


parent_builder = StateGraph(BankState)
parent_builder.add_node("supervisor", supervisor)
parent_builder.add_node("account_agent", account_graph)
parent_builder.add_node("transfer_agent", transfer_graph)

parent_builder.add_edge(START, "supervisor")
parent_builder.add_conditional_edges(
    "supervisor", route_from_supervisor, {"account_agent": "account_agent", "transfer_agent": "transfer_agent"}
)
parent_builder.add_edge("account_agent", END)
parent_builder.add_edge("transfer_agent", END)

checkpoint_serde = JsonPlusSerializer(
    allowed_msgpack_modules=[(TransferAccounts.__module__, TransferAccounts.__name__)]
)
parent_graph = parent_builder.compile(checkpointer=InMemorySaver(serde=checkpoint_serde))
