# 사용자 입력을 반복해서 받고 그래프의 응답을 출력합니다.
# 같은 대화 세션을 유지하고 승인·거절 입력과 종료 명령을 처리합니다.

from langchain_core.messages import HumanMessage
from graph import parent_graph

OWNER_ID = "user-001"
config = {'configurable': {'thread_id': 'session-1'}}

print('잔액 조회 테스트 (종료: quit)')

while True:
    user_input = input('\n나: ').strip()
    if user_input.lower() in ('quit', 'exit', '종료'):
        break
    if not user_input:
        continue

    result = parent_graph.invoke(
        {'messages': [HumanMessage(content=user_input)], 'owner_id': OWNER_ID},
        config=config,
    )

    print(f'\nBot: {result['messages'][-1].text}')