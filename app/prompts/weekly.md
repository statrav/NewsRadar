## 최근 7일 요약 (매일, profile의 weekly가 true일 때 실행 스크립트가 붙인다)

오늘 정리(render)까지 끝낸 뒤에 한다.

1. `{RADAR} week`를 실행한다 → `week-input.json`이 생긴다 (오늘을 포함한 최근 7일)
2. `week-input.json`과 `profile.yaml`을 읽고 `weekly.json`을 쓴다
3. `{RADAR} check --week`가 "문제 없음"이 될 때까지 고친다
4. `{RADAR} render`를 다시 실행한다 (페이지의 '최근 7일' 탭에 들어간다)

## weekly.json 모양
```json
{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "headline": "최근 7일을 한 문장으로",
 "points": [{"name": "흐름 이름", "line": "무슨 일인지 1~2문장", "use": "이 사람 일에 어떻게 쓰나 한 문장", "ids": ["1110-1", "1112-3"]}]}
```

## 규칙
- start·end는 week-input.json의 값 그대로
- points 3~5개 (쌓인 날이 3일 미만이면 1개부터). 여러 날 이어진 이야기, 방향이 바뀐 것 위주로. 하루짜리 소식을 나열하지 않는다
- ids는 week-input.json의 items 키. 요점마다 2~6개
- use는 profile의 `job` 관점. 뻔한 말은 쓰지 않는다
- 어제 쓴 weekly.json과 비슷해도 된다. 오늘 새로 들어온 소식이 흐름을 바꿨으면 반영한다
- week-input.json에 있는 내용으로만 쓴다. 숫자·주장을 지어내지 않는다. 기사 원문 링크는 열지 않는다
- 화면 글은 한국어, 제품·회사 이름만 원래 표기
