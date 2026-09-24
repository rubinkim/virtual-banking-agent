from langchain_core.messages import AIMessage

from graph import account_enforce_owner

fake = AIMessage(
    content="",
    id="test-1",
    tool_calls=[{
        "name": "get_account_balance_by_owner",
        "args": {"owner_id": "user-002", "account_id": "acc-004"},
        "id": "call-1",
    }],
)

out = account_enforce_owner({"messages": [fake], "owner_id": "user-001"})
args = out["messages"][0].tool_calls[0]["args"]
print(args)

assert args["owner_id"] == "user-001"
assert args["account_id"] == "acc-004"
print("통과: owner_id가 user-001로 강제 교체됨")