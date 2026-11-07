import pandas as pd

data = {
    '검색어': [
        '수소상용차 AND (전력분배 OR FDC OR PDU)',
        '무선전력전송 AND (30kW OR 50kW)',
        '("멀티스택" OR "연료전지") AND "전력변환"',
        '("전기버스" OR "상용차") AND ("무선충전" OR "무선전력")'
    ],
    '기준ID': ['Q101', 'Q102', 'Q103', 'Q104']
}

df = pd.DataFrame(data)
df.to_csv('search_queries_additional.csv', index=False, encoding='utf-8-sig')
print("search_queries_additional.csv 생성 완료!")