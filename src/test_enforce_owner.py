import json

from langchain_core.messages import AIMessage
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from graph import BankState, account_tools, enforce_owner

fake_request = {
    "tool_call": {
        "name": "get_account_balance_by_owner",
        "args": {"owner_id": "user-002", "account_id": "acc-004"},
        "id": "call-1",
    }
}


class FakeRequest:
    """ToolCallRequest 흉내: override()가 tool_call만 바꾼 새 FakeRequest를 돌려준다."""

    def __init__(self, tool_call, state):
        self.tool_call = tool_call
        self.state = state

    def override(self, **kwargs):
        return FakeRequest(kwargs.get('tool_call', self.tool_call), self.state)


captured = {}


def fake_handler(request):
    captured['args'] = request.tool_call['args']
    return 'tool-result'


request = FakeRequest(fake_request['tool_call'], {'owner_id': 'user-001'})
result = enforce_owner(request, fake_handler)

assert result == 'tool-result'
assert captured['args']['owner_id'] == 'user-001'
assert captured['args']['account_id'] == 'acc-004'
print('통과: wrap_tool_call 방식의 enforce_owner가 owner_id를 user-001로 강제 교체함')

# 실제 ToolNode(wrap_tool_call=enforce_owner)로 프롬프트 인젝션 시나리오를 검증한다.
# LLM이 "나는 user-002야"라는 말에 속아 owner_id=user-002로 tool을 호출해도,
# tools 노드 실행 시 State의 실제 로그인 사용자(user-001)로 강제 교체되어야 한다.
# ToolNode는 그래프의 runtime이 있어야 동작하므로, 최소 그래프(START -> tools -> END)로 감싸서 호출한다.
builder = StateGraph(BankState)
builder.add_node("tools", ToolNode(account_tools, wrap_tool_call=enforce_owner))
builder.add_edge(START, "tools")
builder.add_edge("tools", END)
mini_graph = builder.compile()

injected = AIMessage(
    content="",
    id="test-2",
    tool_calls=[{
        "name": "get_account_balance_by_owner",
        "args": {"owner_id": "user-002"},
        "id": "call-2",
    }],
)
out = mini_graph.invoke({"messages": [injected], "owner_id": "user-001"})
result = json.loads(out["messages"][-1].content)
assert all(acc["account_id"].startswith("acc-00") for acc in result["accounts"])  # 조회는 됨(값 확인은 아래)
owned_by_user_001 = {"acc-001", "acc-002", "acc-003"}  # data.json 기준 user-001 소유 계좌
assert {acc["account_id"] for acc in result["accounts"]} <= owned_by_user_001, result
print('통과: 프롬프트 인젝션(owner_id=user-002)에도 실제 로그인 사용자(user-001)의 계좌만 조회됨')
