# 업무 흐름에 필요한 State와 노드 함수를 정의합니다.
# 요청 해석, 업무 함수 호출, 승인·거절과 결과 안내를 연결합니다.
# LangGraph의 중단·재개로 사용자 승인을 처리합니다.

import os
from dotenv import load_dotenv

from typing import Annotated, TypedDict, Literal
from pydantic import BaseModel

from langgraph.graph.message import add_messages
from langchain_core.messages import SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode, tools_condition
from langgraph.checkpoint.memory import InMemorySaver

from functions import get_account_balance_by_owner, get_account_transactions_by_owner, get_base_date

load_dotenv()
GEMINI_MODEL = os.getenv('GEMINI_MODEL')

account_tools = [get_account_balance_by_owner, get_account_transactions_by_owner]

llm = ChatGoogleGenerativeAI(model=GEMINI_MODEL)
llm_with_tools = llm.bind_tools(account_tools)


class BankState(TypedDict):
    messages: Annotated[list, add_messages]
    owner_id: str
    next: str

class RouterDecision(BaseModel):
    next: Literal["account_agent"]

router_llm = llm.with_structured_output(RouterDecision)


def supervisor(state: BankState) -> BankState:
    """사용자 요청을 분석해서 어떤 subagent로 보낼지 결정하는 노드"""

    messages = state['messages']
    system = SystemMessage(
        content=(
            "당신은 은행 업무 요청을 분석해서 적절한 담당 agent로 routing하는 supervisor 입니다. "
            "현재는 계좌 조회, 잔액 조회, 거래내역 조회, 이체, 계좌 별명 변경을 담당하는 account_agent만 있습니다. "
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
            f"당신은 accounts domain(은행계좌잔액조회, 은행거래내역조회, 은행계좌간 이체, 은행계좌 nickname 변경)안의 업무를 처리하는 agent입니다. "
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


parent_builder = StateGraph(BankState)
parent_builder.add_node("supervisor", supervisor)
parent_builder.add_node("account_agent", account_graph)

parent_builder.add_edge(START, "supervisor")
parent_builder.add_conditional_edges(
    "supervisor", route_from_supervisor, {"account_agent": "account_agent"}
)
parent_builder.add_edge("account_agent", END)

parent_graph = parent_builder.compile(checkpointer=InMemorySaver())
