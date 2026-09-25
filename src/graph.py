# 업무 흐름에 필요한 State와 노드 함수를 정의합니다.
# 요청 해석, 업무 함수 호출, 승인·거절과 결과 안내를 연결합니다.
# LangGraph의 중단·재개로 사용자 승인을 처리합니다.

import os
from dotenv import load_dotenv

from datetime import datetime
from typing import Annotated, TypedDict, Literal
from pydantic import BaseModel, Field, ValidationError

from langgraph.graph.message import add_messages
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
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
    transfer_response: str | None
    transfer_decision: str | None
    transfer_notice: str | None


class RouterDecision(BaseModel):
    next: Literal["account_agent", "transfer_agent", "unsupported"]

router_llm = llm.with_structured_output(RouterDecision)


def supervisor(state: BankState) -> BankState:
    """사용자 요청을 분석해서 어떤 subagent로 보낼지 결정하는 노드"""

    messages = state['messages']
    system = SystemMessage(
        content=(
            "당신은 은행 업무 요청을 분석해서 적절한 담당 agent로 routing하는 supervisor 입니다. "
            "담당 agent는 두 가지입니다. "
            "account_agent: 계좌 목록·잔액 조회, 거래내역 조회(카드로 결제한 거래내역·카드 사용 내역 조회도 여기에 포함). "
            "transfer_agent: 사용자 본인의 계좌 사이의 이체. 이체 정보(출금 계좌, 입금 계좌, 금액)를 되물은 직후 사용자가 그에 답하는 경우도 transfer_agent입니다. "
            "unsupported: 위 두 agent가 처리하지 못하는 요청(카드 자체의 정지·잠금·재발급·목록·상태 조회, 청구서 조회·납부, 계좌 별명 변경, 일상 대화 등). "
            "단, 카드로 결제한 '거래내역'을 묻는 것은 unsupported가 아니라 account_agent입니다. "
            "억지로 가장 가까운 agent에 보내지 말고, 처리할 수 없는 요청이면 unsupported를 반환하세요. "
            "해당하는 agent의 이름을 그대로 반환하세요. "
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

TRANSFER_RESET = {'transfer': None, 'transfer_response': None, 'transfer_decision': None, 'transfer_notice': None}
TRANSFER_CANCEL_TEXT = '이체를 취소했습니다. 잔액과 거래내역은 변경되지 않았습니다.'


def transfer_end(text: str) -> BankState:
    """이체 흐름을 끝내는 공통 처리: 안내 메시지를 남기고 이체 관련 State를 전부 비운다"""
    return {'messages': [AIMessage(content=text)], **TRANSFER_RESET}


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
        return transfer_end(text)

    try:
        transfer = TransferAccounts(from_account=draft.from_account, to_account=draft.to_account, amount=draft.amount)
    except ValidationError:
        return transfer_end('이체 금액은 0보다 큰 원 단위 정수여야 합니다. 금액을 다시 말씀해 주세요.')
    return {**TRANSFER_RESET, 'transfer': transfer}


def transfer_validate(state: BankState) -> BankState:
    """이체할 수 있는 요청인지 검증하는 노드. 실패하면 이유를 안내하고 State.transfer를 비운다"""

    transfer = state['transfer']
    reason = validate_transfer(load_data(), state['owner_id'], transfer.from_account, transfer.to_account, transfer.amount)
    if reason:
        return transfer_end(f'이체를 진행할 수 없습니다. {reason}')
    return {}


def transfer_approve(state: BankState) -> BankState:
    """이체안 전체를 보여주고 interrupt로 사용자의 응답 원문을 받아 State에 저장하는 노드"""

    transfer = state['transfer']
    preview = build_transfer_preview(load_data(), state['owner_id'], transfer.from_account, transfer.to_account, transfer.amount)
    source, target = preview['from'], preview['to']
    notice = state.get('transfer_notice')
    message = (
        (f'{notice}\n\n' if notice else '')
        + '다음 이체를 진행할까요?\n'
        f"- 출금: {source['nickname']}({source['account_id']}) {source['balance_before']:,}원 → {source['balance_after']:,}원\n"
        f"- 입금: {target['nickname']}({target['account_id']}) {target['balance_before']:,}원 → {target['balance_after']:,}원\n"
        f"- 이체 금액: {preview['amount']:,}원"
    )
    response = interrupt({'message': message, 'preview': preview})

    return {'transfer_response': response, 'transfer_notice': None, 'transfer_decision': None}


class ResponseInterpretation(BaseModel):
    action: Literal['approve', 'reject', 'edit', 'unclear'] = Field(
        description="approve: 이체안을 그대로 진행하는 데 분명히 동의 / reject: 이체하지 않겠다 / edit: 이체안의 일부를 바꾸겠다 / unclear: 분명하지 않음"
    )
    from_account: str | None = Field(default=None, description="바꾸려는 출금 계좌의 account_id. 바꾸지 않으면 null")
    to_account: str | None = Field(default=None, description="바꾸려는 입금 계좌의 account_id. 바꾸지 않으면 null")
    amount: int | None = Field(default=None, description="바꾸려는 이체 금액(원 단위 정수). 바꾸지 않으면 null")


transfer_interpret_llm = llm.with_structured_output(ResponseInterpretation)

FAST_APPROVE_WORDS = {'승인'}
FAST_REJECT_WORDS = {'거절'}
TRANSFER_EDIT_FIELDS = ('from_account', 'to_account', 'amount')
TRANSFER_UNCLEAR_NOTICE = "응답을 이해하지 못했습니다. '승인' 또는 '거절'이라고 하시거나, 바꾸고 싶은 내용을 말씀해 주세요."


def classify_response(state: BankState) -> ResponseInterpretation:
    """승인 화면에 대한 사용자의 자연어 응답을 LLM으로 분류한다"""

    transfer = state['transfer']
    accounts = get_owner_accounts(load_data(), state['owner_id'])
    nickname = {a['account_id']: a['nickname'] for a in accounts}
    account_lines = '\n'.join(f"- {a['account_id']}: {a['nickname']}" for a in accounts)
    plan = (
        f"- 출금 계좌: {nickname.get(transfer.from_account, '알 수 없음')}({transfer.from_account})\n"
        f"- 입금 계좌: {nickname.get(transfer.to_account, '알 수 없음')}({transfer.to_account})\n"
        f"- 이체 금액: {transfer.amount:,}원"
    )
    system = SystemMessage(
        content=(
            "당신은 은행 이체 승인 화면에 대한 사용자의 응답을 분류합니다. "
            f"현재 이체안:\n{plan}\n사용자의 계좌 목록:\n{account_lines}\n"
            "approve는 사용자가 이 이체안을 그대로 진행하는 데 분명하게 동의할 때만 선택하세요. "
            "이체안을 바꾸겠다는 말이 조금이라도 섞여 있으면(예: '5만 원으로 하고 진행해') approve가 아니라 edit입니다. "
            "reject는 이체를 하지 않겠다는 뜻입니다. edit는 출금 계좌, 입금 계좌, 금액 중 하나 이상을 바꾸겠다는 뜻입니다. "
            "동의인지 거절인지 수정인지 분명하지 않으면 반드시 unclear를 선택하세요. "
            "edit일 때는 사용자가 바꾼 값만 채우고 나머지는 null로 두세요. "
            "별명은 계좌 목록에서 account_id로 바꾸고, 금액은 원 단위 정수로 바꾸세요(예: 5만 원은 50000). "
            "사용자가 account_id를 직접 말했다면 목록에 없더라도 그대로 반환하세요."
        )
    )
    return transfer_interpret_llm.invoke([system, HumanMessage(content=state['transfer_response'])])


def make_transfer_interpret(classifier=classify_response):
    """사용자의 응답을 승인·거절·수정·불명확으로 해석하는 노드를 만든다. LLM 분류 뒤의 규칙은 전부 코드가 지킨다"""

    def retry(decision: str, notice: str, transfer=None) -> BankState:
        update = {'transfer_response': None, 'transfer_decision': decision, 'transfer_notice': notice}
        if transfer is not None:
            update['transfer'] = transfer
        return update

    def transfer_interpret(state: BankState) -> BankState:
        response = (state.get('transfer_response') or '').strip()
        transfer = state['transfer']

        if response in FAST_APPROVE_WORDS:
            return {'transfer_response': None, 'transfer_decision': 'approve'}
        if response in FAST_REJECT_WORDS:
            return transfer_end(TRANSFER_CANCEL_TEXT)

        try:
            result = classifier(state)
        except Exception:
            result = None
        if result is None:
            return retry('unclear', TRANSFER_UNCLEAR_NOTICE)

        changes = {key: getattr(result, key) for key in TRANSFER_EDIT_FIELDS if getattr(result, key) is not None}
        action = result.action
        if action == 'approve' and changes:
            action = 'edit'

        if action == 'approve':
            return {'transfer_response': None, 'transfer_decision': 'approve'}
        if action == 'reject':
            return transfer_end(TRANSFER_CANCEL_TEXT)
        if action != 'edit':
            return retry('unclear', TRANSFER_UNCLEAR_NOTICE)

        current = transfer.model_dump(exclude={'date'})
        if not changes:
            return retry('unclear', '수정할 내용을 파악하지 못했습니다. 바꾸고 싶은 계좌나 금액을 다시 말씀해 주세요.')
        if {**current, **changes} == current:
            return retry('unclear', '말씀하신 내용은 현재 이체안과 같습니다. 바꾸고 싶은 내용이 있으면 다시 말씀해 주세요.')

        try:
            candidate = TransferAccounts(**{**current, **changes})
        except ValidationError:
            return retry('edit', '요청하신 수정은 반영할 수 없습니다. 이체 금액은 0보다 큰 원 단위 정수여야 합니다. 기존 이체안을 그대로 유지합니다.')

        reason = validate_transfer(load_data(), state['owner_id'], candidate.from_account, candidate.to_account, candidate.amount)
        if reason:
            return retry('edit', f'요청하신 수정은 반영할 수 없습니다. {reason} 기존 이체안을 그대로 유지합니다.')
        return retry('edit', '수정한 내용을 반영했습니다. 바뀐 이체안을 다시 확인해 주세요.', transfer=candidate)

    return transfer_interpret


transfer_interpret = make_transfer_interpret()


def transfer_execute(state: BankState) -> BankState:
    """승인된 이체를 다시 검증한 뒤 잔액·거래내역·처리 기록을 한 번에 저장하는 노드"""

    if state.get('transfer_decision') != 'approve':
        return transfer_end('승인이 확인되지 않아 이체를 진행하지 않았습니다. 잔액과 거래내역은 변경되지 않았습니다.')

    transfer = state['transfer']
    owner_id = state['owner_id']
    data = load_data()

    reason = validate_transfer(data, owner_id, transfer.from_account, transfer.to_account, transfer.amount)
    if reason:
        return transfer_end(f'승인 이후 다시 확인해 보니 이체를 진행할 수 없습니다. {reason}')

    completed = transfer.model_copy(update={'date': get_transfer_time()})
    try:
        new_data, record = apply_transfer(data, owner_id, completed.from_account, completed.to_account, completed.amount, completed.date)
        save_data(new_data)
    except Exception:
        return transfer_end('저장 중 오류가 발생해 이체가 반영되지 않았습니다. 잔액과 거래내역은 그대로입니다.')

    balances = {a['account_id']: a for a in new_data['accounts']}
    source, target = balances[completed.from_account], balances[completed.to_account]
    text = (
        '이체가 완료되었습니다.\n'
        f"- {source['nickname']} → {target['nickname']}: {completed.amount:,}원\n"
        f"- 이체 후 잔액: {source['nickname']} {source['balance']:,}원, {target['nickname']} {target['balance']:,}원\n"
        f"- 처리 번호: {record['request_id']}"
    )
    return transfer_end(text)


def transfer_continues(state: BankState) -> str:
    return 'continue' if state.get('transfer') is not None else END


def transfer_after_interpret(state: BankState) -> str:
    if state.get('transfer') is None:
        return END
    return 'execute' if state.get('transfer_decision') == 'approve' else 'again'


def build_transfer_graph(extract=transfer_extract, classifier=classify_response):
    builder = StateGraph(BankState)
    builder.add_node("transfer_extract", extract)
    builder.add_node("transfer_validate", transfer_validate)
    builder.add_node("transfer_approve", transfer_approve)
    builder.add_node("transfer_interpret", make_transfer_interpret(classifier))
    builder.add_node("transfer_execute", transfer_execute)

    builder.add_edge(START, "transfer_extract")
    builder.add_conditional_edges("transfer_extract", transfer_continues, {"continue": "transfer_validate", END: END})
    builder.add_conditional_edges("transfer_validate", transfer_continues, {"continue": "transfer_approve", END: END})
    builder.add_edge("transfer_approve", "transfer_interpret")
    builder.add_conditional_edges(
        "transfer_interpret", transfer_after_interpret,
        {"execute": "transfer_execute", "again": "transfer_approve", END: END},
    )
    builder.add_edge("transfer_execute", END)
    return builder.compile()


transfer_graph = build_transfer_graph()


UNSUPPORTED_TEXT = (
    '죄송합니다. 해당 요청은 아직 지원하지 않습니다. '
    '현재는 계좌·잔액 조회, 거래내역 조회, 내 계좌 간 이체만 도와드릴 수 있습니다.'
)


def unsupported_reply(state: BankState) -> BankState:
    """지원하지 않는 요청에 정해진 문장으로 안내하는 노드"""
    return {'messages': [AIMessage(content=UNSUPPORTED_TEXT)]}


parent_builder = StateGraph(BankState)
parent_builder.add_node("supervisor", supervisor)
parent_builder.add_node("account_agent", account_graph)
parent_builder.add_node("transfer_agent", transfer_graph)
parent_builder.add_node("unsupported", unsupported_reply)

parent_builder.add_edge(START, "supervisor")
parent_builder.add_conditional_edges(
    "supervisor", route_from_supervisor,
    {"account_agent": "account_agent", "transfer_agent": "transfer_agent", "unsupported": "unsupported"},
)
parent_builder.add_edge("account_agent", END)
parent_builder.add_edge("transfer_agent", END)
parent_builder.add_edge("unsupported", END)

checkpoint_serde = JsonPlusSerializer(
    allowed_msgpack_modules=[(TransferAccounts.__module__, TransferAccounts.__name__)]
)
parent_graph = parent_builder.compile(checkpointer=InMemorySaver(serde=checkpoint_serde))
