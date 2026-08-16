"""国家、账单资料、语言环境与浏览器配置目录。

这里仅保存静态资料和无状态解析函数，支付流程与界面可以共同复用。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit


COUNTRY_CURRENCY = {
    "AT": "EUR", "AU": "AUD", "BE": "EUR", "BR": "BRL", "CA": "CAD", "CH": "CHF", "CZ": "CZK",
    "DE": "EUR", "DK": "DKK", "ES": "EUR", "FI": "EUR", "FR": "EUR", "GB": "GBP", "HK": "HKD",
    "ID": "IDR", "IE": "EUR", "IN": "INR", "IT": "EUR", "JP": "JPY", "KR": "KRW", "MX": "MXN",
    "MY": "MYR", "NL": "EUR", "NO": "NOK", "NZ": "NZD", "PH": "PHP", "PL": "PLN", "PT": "EUR",
    "SE": "SEK", "SG": "SGD", "TH": "THB", "TW": "TWD", "US": "USD", "VN": "VND",
}
OPENAI_SUPPORTED_COUNTRY_CODES = {
    "AX", "AL", "DZ", "AS", "AD", "AO", "AI", "AQ", "AG", "AR",
    "AM", "AW", "AU", "AT", "AZ", "BS", "BH", "BD", "BB", "BE",
    "BZ", "BJ", "BM", "BT", "BO", "BQ", "BA", "BW", "BV", "BR",
    "IO", "BN", "BG", "BF", "BI", "CV", "KH", "CM", "CA", "KY",
    "CF", "TD", "CL", "CX", "CC", "CO", "KM", "CG", "CK", "CR",
    "CI", "HR", "CW", "CY", "CZ", "DK", "DJ", "DM", "DO", "EC",
    "SV", "GQ", "ER", "EE", "SZ", "FK", "FO", "FJ", "FI", "FR",
    "GF", "PF", "TF", "GA", "GM", "GE", "DE", "GH", "GI", "GR",
    "GL", "GD", "GP", "GU", "GT", "GG", "GN", "GW", "GY", "HT",
    "HM", "VA", "HN", "HU", "IS", "IN", "ID", "IQ", "IE", "IM",
    "IL", "IT", "JM", "JP", "JE", "JO", "KZ", "KE", "KI", "KW",
    "KG", "LA", "LV", "LB", "LS", "LR", "LI", "LT", "LU", "MG",
    "MW", "MY", "MV", "ML", "MT", "MH", "MQ", "MR", "MU", "YT",
    "MX", "FM", "MD", "MC", "MN", "ME", "MS", "MA", "MZ", "MM",
    "NA", "NR", "NP", "NL", "NC", "NZ", "NI", "NE", "NG", "NU",
    "NF", "MK", "MP", "NO", "OM", "PK", "PW", "PS", "PA", "PG",
    "PE", "PH", "PN", "PL", "PT", "PR", "QA", "RE", "RO", "RW",
    "BL", "SH", "KN", "LC", "MF", "PM", "VC", "WS", "SM", "ST",
    "SN", "RS", "SC", "SL", "SG", "SX", "SK", "SI", "SB", "SO",
    "ZA", "GS", "KR", "SS", "ES", "LK", "SR", "SJ", "SE", "CH",
    "TW", "TZ", "TH", "TL", "TG", "TK", "TO", "TT", "TN", "TR",
    "TM", "TC", "TV", "UG", "UA", "AE", "GB", "UM", "US", "UY",
    "UZ", "VU", "VN", "WF", "EH", "ZM",
}
EUR_COUNTRIES = {
    "AD", "AT", "BE", "CY", "EE", "FI", "FR", "DE", "GR", "HR",
    "IE", "IT", "LV", "LT", "LU", "MT", "MC", "ME", "NL", "PT",
    "SM", "SK", "SI", "ES",
}
COUNTRY_CURRENCY.update({country: "EUR" for country in EUR_COUNTRIES if country not in COUNTRY_CURRENCY})
COUNTRY_CURRENCY.update({
    "AE": "AED", "AR": "ARS", "BH": "BHD", "BM": "BMD", "BO": "BOB", "BQ": "USD",
    "CL": "CLP", "CO": "COP", "GU": "USD", "IL": "ILS", "PR": "USD", "TR": "TRY",
    "UA": "UAH", "UM": "USD", "ZA": "ZAR",
})
COUNTRY_PHONE_PREFIX = {
    "AU": "+61", "CA": "+1", "DE": "+49", "GB": "+44", "IE": "+353", "JP": "+81",
    "NZ": "+64", "SG": "+65", "TH": "+66", "US": "+1", "VN": "+84",
    "AD": "+376", "AE": "+971", "AL": "+355", "AR": "+54", "AT": "+43", "BE": "+32",
    "BG": "+359", "BH": "+973", "BM": "+1", "BO": "+591", "BR": "+55", "CH": "+41",
    "CL": "+56", "CO": "+57", "CR": "+506", "CY": "+357", "CZ": "+420", "DK": "+45",
    "EE": "+372", "ES": "+34", "FI": "+358", "FR": "+33", "GI": "+350", "GR": "+30",
    "HK": "+852", "HU": "+36", "ID": "+62", "IL": "+972", "IN": "+91", "IS": "+354",
    "IT": "+39", "KR": "+82", "KZ": "+7", "LI": "+423", "LT": "+370", "LU": "+352",
    "LV": "+371", "MC": "+377", "MD": "+373", "ME": "+382", "MK": "+389", "MT": "+356",
    "MX": "+52", "MY": "+60", "NL": "+31", "NO": "+47", "PH": "+63", "PL": "+48",
    "PT": "+351", "QA": "+974", "RO": "+40", "RS": "+381", "SA": "+966", "SE": "+46",
    "SI": "+386", "SK": "+421", "SM": "+378", "TR": "+90", "TW": "+886", "UA": "+380",
    "UY": "+598", "ZA": "+27",
}
US_BILLING_NAMES = [("James", "Smith"), ("John", "Brown"), ("Michael", "Johnson"), ("Robert", "Miller"), ("David", "Davis"), ("William", "Wilson")]
US_BILLING_STREETS = [
    ("3110 Sunset Boulevard", "Los Angeles", "CA", "90026"),
    ("1200 Market Street", "San Francisco", "CA", "94102"),
    ("500 Main Street", "Austin", "TX", "78701"),
    ("88 Broadway", "New York", "NY", "10007"),
    ("1200 Peachtree St", "Atlanta", "GA", "30309"),
]
DE_BILLING_NAMES = [("Lukas", "Schneider"), ("Felix", "Muller"), ("Jonas", "Weber"), ("Leon", "Fischer"), ("Marie", "Wagner"), ("Laura", "Becker"), ("Maximilian", "Hoffmann"), ("Paul", "Schulz"), ("Emma", "Koch"), ("Hannah", "Bauer"), ("Sophie", "Richter"), ("Noah", "Klein")]
DE_BILLING_STREETS = [
    ("Friedrichstrasse 123", "Berlin", "BE", "10117"),
    ("Leopoldstrasse 50", "Munich", "BY", "80802"),
    ("Zeil 85", "Frankfurt am Main", "HE", "60313"),
    ("Konigsallee 60", "Dusseldorf", "NW", "40212"),
    ("Moenckebergstrasse 7", "Hamburg", "HH", "20095"),
    ("Hohenzollernring 72", "Cologne", "NW", "50672"),
    ("Kaiserstrasse 44", "Stuttgart", "BW", "70173"),
    ("Kaufingerstrasse 15", "Munich", "BY", "80331"),
    ("Georgstrasse 24", "Hanover", "NI", "30159"),
    ("Prager Strasse 9", "Dresden", "SN", "01069"),
    ("Schadowstrasse 36", "Dusseldorf", "NW", "40212"),
    ("Breite Strasse 18", "Bonn", "NW", "53111"),
]
GB_BILLING_NAMES = [("Oliver", "Smith"), ("George", "Taylor"), ("Harry", "Brown"), ("Noah", "Wilson"), ("Jack", "Davies"), ("Arthur", "Evans"), ("Olivia", "Johnson"), ("Amelia", "Roberts"), ("Isla", "Walker"), ("Ava", "Thompson"), ("Mia", "White"), ("Grace", "Hughes")]
GB_BILLING_STREETS = [
    ("221B Baker Street", "London", "England", "NW1 6XE"),
    ("10 Downing Street", "London", "England", "SW1A 2AA"),
    ("45 Deansgate", "Manchester", "England", "M3 2AY"),
    ("18 Park Row", "Leeds", "England", "LS1 5JA"),
    ("77 Queen Street", "Cardiff", "Wales", "CF10 2GR"),
    ("9 Princes Street", "Edinburgh", "Scotland", "EH2 2ER"),
    ("33 Broad Street", "Birmingham", "England", "B1 2HF"),
    ("14 Castle Street", "Liverpool", "England", "L2 0NE"),
    ("52 College Green", "Bristol", "England", "BS1 5SH"),
    ("6 Royal Avenue", "Belfast", "Northern Ireland", "BT1 1DA"),
]
AU_BILLING_NAMES = [("Jack", "Wilson"), ("Oliver", "Taylor"), ("Noah", "Brown"), ("Charlotte", "Smith"), ("Amelia", "Jones"), ("Isla", "Williams")]
AU_BILLING_STREETS = [
    ("120 Collins Street", "Melbourne", "Victoria", "3000"),
    ("88 George Street", "Sydney", "New South Wales", "2000"),
    ("45 Queen Street", "Brisbane", "Queensland", "4000"),
    ("22 King William Street", "Adelaide", "South Australia", "5000"),
    ("60 St Georges Terrace", "Perth", "Western Australia", "6000"),
    ("18 Elizabeth Street", "Hobart", "Tasmania", "7000"),
]
EXTRA_BILLING_NAMES = [("Alex", "Tan"), ("Daniel", "Lee"), ("Emma", "Wong"), ("Mia", "Chen"), ("Noah", "Martin"), ("Olivia", "Nguyen")]
# 荷兰 iDEAL 专用资料：姓名本地化，地址/城市/省份/邮编保持成套，避免通用生成器
# 产生“随机街道 + 不属于该城市的随机邮编 + state=NL”这类明显不一致资料。
NL_BILLING_NAMES = [
    ("Jan", "de Vries"), ("Daan", "Jansen"), ("Pieter", "de Jong"),
    ("Bram", "Bakker"), ("Lars", "Visser"), ("Ruben", "Smit"),
    ("Sophie", "Meijer"), ("Emma", "de Boer"), ("Eva", "Mulder"),
    ("Femke", "de Groot"), ("Lotte", "Bos"), ("Noor", "Vos"),
]
NL_BILLING_STREETS = [
    ("Damrak 1", "Amsterdam", "Noord-Holland", "1012 LG"),
    ("Coolsingel 40", "Rotterdam", "Zuid-Holland", "3011 AD"),
    ("Neude 11", "Utrecht", "Utrecht", "3512 AE"),
    ("Grote Marktstraat 10", "Den Haag", "Zuid-Holland", "2511 BJ"),
    ("Stationsplein 22", "Eindhoven", "Noord-Brabant", "5611 AC"),
    ("Grote Markt 1", "Groningen", "Groningen", "9712 HN"),
    ("Stationsplein 1", "Amersfoort", "Utrecht", "3818 LE"),
    ("Markt 1", "Maastricht", "Limburg", "6211 CH"),
    ("Waagplein 2", "Alkmaar", "Noord-Holland", "1811 JP"),
    ("Plein 1944 1", "Nijmegen", "Gelderland", "6511 JB"),
]
# 韩国 Kakao Pay 专用资料：姓名、道路、行政区和邮编成套使用。
KR_BILLING_NAMES = [
    ("김", "민준"), ("이", "서준"), ("박", "도윤"), ("최", "예준"),
    ("정", "서연"), ("강", "서윤"), ("조", "지우"), ("윤", "하은"),
]
KR_BILLING_STREETS = [
    ("테헤란로 87", "서울특별시", "강남구", "06164"),
    ("봉은사로 524", "서울특별시", "강남구", "06097"),
    ("서초대로 396", "서울특별시", "서초구", "06611"),
    ("올림픽로 300", "서울특별시", "송파구", "05510"),
    ("월드컵북로 396", "서울특별시", "마포구", "03925"),
]
# 越南 MoMo 专用资料：城市、省市和 6 位邮编保持成套，供 Checkout taxes/tax_region 使用。
VN_BILLING_NAMES = [
    ("Nguyen", "Minh Anh"), ("Tran", "Quoc Bao"), ("Le", "Hoang Nam"),
    ("Pham", "Thu Ha"), ("Vo", "Gia Huy"), ("Bui", "Thanh Mai"),
]
VN_BILLING_STREETS = [
    ("12 Nguyen Hue", "Ho Chi Minh City", "Ho Chi Minh", "700000"),
    ("45 Le Loi", "Ho Chi Minh City", "Ho Chi Minh", "700000"),
    ("18 Trang Tien", "Hanoi", "Hanoi", "100000"),
    ("36 Hoang Dieu", "Hanoi", "Hanoi", "100000"),
    ("25 Bach Dang", "Da Nang", "Da Nang", "550000"),
    ("88 Tran Phu", "Da Nang", "Da Nang", "550000"),
]
# 印度专用：常见英文转写姓名 + 真实商圈/地标地址（邮编与城市匹配，格式合格供 Stripe billing）
IN_BILLING_NAMES = [
    ("Aarav", "Sharma"), ("Vivaan", "Patel"), ("Aditya", "Singh"), ("Vihaan", "Gupta"),
    ("Arjun", "Reddy"), ("Sai", "Iyer"), ("Krishna", "Nair"), ("Rohan", "Mehta"),
    ("Kabir", "Joshi"), ("Ishaan", "Desai"), ("Aryan", "Malhotra"), ("Dev", "Chopra"),
    ("Ananya", "Sharma"), ("Aadhya", "Patel"), ("Diya", "Singh"), ("Myra", "Gupta"),
    ("Anika", "Reddy"), ("Sara", "Iyer"), ("Kiara", "Nair"), ("Pari", "Mehta"),
    ("Saanvi", "Joshi"), ("Ira", "Desai"), ("Navya", "Malhotra"), ("Riya", "Chopra"),
    ("Rahul", "Verma"), ("Amit", "Kulkarni"), ("Priya", "Banerjee"), ("Neha", "Agarwal"),
    ("Siddharth", "Menon"), ("Kavya", "Pillai"), ("Manish", "Bhatia"), ("Pooja", "Saxena"),
]
IN_BILLING_STREETS = [
    # (line1, city, state, postal_code) — 6 位 pincode 与城市对应
    ("12 Connaught Place", "New Delhi", "Delhi", "110001"),
    ("A-42 Defence Colony", "New Delhi", "Delhi", "110024"),
    ("15 Khan Market", "New Delhi", "Delhi", "110003"),
    ("88 Nehru Place", "New Delhi", "Delhi", "110019"),
    ("24 MG Road", "Bengaluru", "Karnataka", "560001"),
    ("42 Indiranagar 100 Feet Road", "Bengaluru", "Karnataka", "560038"),
    ("7 Koramangala 5th Block", "Bengaluru", "Karnataka", "560095"),
    ("18 Cubbon Road", "Bengaluru", "Karnataka", "560001"),
    ("18 Linking Road", "Mumbai", "Maharashtra", "400050"),
    ("14 Bandra Kurla Complex", "Mumbai", "Maharashtra", "400051"),
    ("32 Colaba Causeway", "Mumbai", "Maharashtra", "400005"),
    ("9 Andheri West SV Road", "Mumbai", "Maharashtra", "400058"),
    ("22 Park Street", "Kolkata", "West Bengal", "700016"),
    ("5 Camac Street", "Kolkata", "West Bengal", "700017"),
    ("11 Anna Salai", "Chennai", "Tamil Nadu", "600002"),
    ("45 T Nagar Usman Road", "Chennai", "Tamil Nadu", "600017"),
    ("8 Banjara Hills Road No 12", "Hyderabad", "Telangana", "500034"),
    ("27 Jubilee Hills Road No 36", "Hyderabad", "Telangana", "500033"),
    ("16 FC Road", "Pune", "Maharashtra", "411004"),
    ("4 Koregaon Park North Main Road", "Pune", "Maharashtra", "411001"),
    ("33 CG Road", "Ahmedabad", "Gujarat", "380009"),
    ("19 SG Highway", "Ahmedabad", "Gujarat", "380054"),
    ("6 MI Road", "Jaipur", "Rajasthan", "302001"),
    ("21 Civil Lines", "Jaipur", "Rajasthan", "302006"),
    ("55 Sector 17", "Chandigarh", "Chandigarh", "160017"),
    ("3 Hazratganj", "Lucknow", "Uttar Pradesh", "226001"),
    ("12 Gomti Nagar Vineet Khand", "Lucknow", "Uttar Pradesh", "226010"),
    ("40 Ashram Road", "Ahmedabad", "Gujarat", "380009"),
]
EXTRA_BILLING_STREETS = {
    "TH": [("999 Rama I Road", "Bangkok", "Bangkok", "10330"), ("88 Sukhumvit Road", "Bangkok", "Bangkok", "10110"), ("45 Nimman Road", "Chiang Mai", "Chiang Mai", "50200")],
    "JP": [("1-1 Marunouchi", "Chiyoda-ku", "Tokyo", "100-0005"), ("2-2-1 Yaesu", "Chuo-ku", "Tokyo", "104-0028"), ("3-1 Umeda", "Osaka", "Osaka", "530-0001")],
    "SG": [("10 Anson Road", "Singapore", "Singapore", "079903"), ("1 Raffles Place", "Singapore", "Singapore", "048616"), ("80 Robinson Road", "Singapore", "Singapore", "068898")],
    "NZ": [("22 Queen Street", "Auckland", "Auckland", "1010"), ("50 Lambton Quay", "Wellington", "Wellington", "6011"), ("120 Hereford Street", "Christchurch", "Canterbury", "8011")],
    "CA": [("100 King Street West", "Toronto", "ON", "M5X 1A9"), ("555 West Hastings Street", "Vancouver", "BC", "V6B 4N6"), ("1250 Rene-Levesque Blvd", "Montreal", "QC", "H3B 4W8")],
    "IE": [("1 Grand Canal Square", "Dublin", "Dublin", "D02 P820"), ("10 South Mall", "Cork", "Cork", "T12 RD43"), ("5 Eyre Square", "Galway", "Galway", "H91 FPK2")],
    # 保留少量条目作兜底；UPI 主路径走 IN_BILLING_STREETS
    "IN": [("12 Connaught Place", "New Delhi", "Delhi", "110001"), ("24 MG Road", "Bengaluru", "Karnataka", "560001"), ("18 Linking Road", "Mumbai", "Maharashtra", "400050")],
}
BILLING_PROFILE_CITY_BY_COUNTRY = {
    "AT": ["Vienna", "Graz", "Linz"], "BE": ["Brussels", "Antwerp", "Ghent"], "BR": ["Sao Paulo", "Rio de Janeiro", "Brasilia"],
    "CH": ["Zurich", "Geneva", "Basel"], "DK": ["Copenhagen", "Aarhus", "Odense"], "ES": ["Madrid", "Barcelona", "Valencia"],
    "FI": ["Helsinki", "Espoo", "Tampere"], "FR": ["Paris", "Lyon", "Marseille"], "ID": ["Jakarta", "Surabaya", "Bandung"],
    "IT": ["Rome", "Milan", "Turin"], "KR": ["Seoul", "Busan", "Incheon"], "MX": ["Mexico City", "Guadalajara", "Monterrey"],
    "NL": ["Amsterdam", "Rotterdam", "Utrecht"], "NO": ["Oslo", "Bergen", "Trondheim"], "PL": ["Warsaw", "Krakow", "Gdansk"],
    "PT": ["Lisbon", "Porto", "Coimbra"], "SE": ["Stockholm", "Gothenburg", "Malmo"], "TW": ["Taipei", "Taichung", "Kaohsiung"],
    "VN": ["Ho Chi Minh City", "Hanoi", "Da Nang"],
}
POSTAL_PATTERN_BY_COUNTRY = {
    "AD": "AD###", "AR": "C####", "AU": "####", "AT": "####", "BE": "####", "BR": "#####-###",
    "CA": "A#A #A#", "CH": "####", "CL": "#######", "CZ": "### ##", "DE": "#####", "DK": "####",
    "ES": "#####", "FI": "#####", "FR": "#####", "GB": "AA# #AA", "IE": "A## A###", "ID": "#####",
    "IN": "######", "IT": "#####", "JP": "###-####", "KR": "#####", "MX": "#####", "NL": "#### AA",
    "NO": "####", "NZ": "####", "PL": "##-###", "PT": "####-###", "SE": "### ##", "SG": "######",
    "TH": "#####", "US": "#####", "VN": "######",
}
BILLING_STREET_POOL = ["Market Street", "Central Avenue", "Station Road", "Main Street", "High Street", "King Street"]
BILLING_PROFILE_BY_COUNTRY = {
    country: {
        "currency": COUNTRY_CURRENCY.get(country, "USD"),
        "phone_prefix": COUNTRY_PHONE_PREFIX.get(country, "+1"),
        "city_pool": BILLING_PROFILE_CITY_BY_COUNTRY.get(country, ["Capital City", "Central District", "Market Town"]),
        "postal_pattern": POSTAL_PATTERN_BY_COUNTRY.get(country, "#####"),
        "street_pool": BILLING_STREET_POOL,
    }
    for country in OPENAI_SUPPORTED_COUNTRY_CODES
}
LOCALE_MAP = {
    "de": ("de-DE", "de"),
    "de-DE": ("de-DE", "de"),
    "en": ("en-US", "en"),
    "en-US": ("en-US", "en"),
    "en-GB": ("en-GB", "en"),
    "en-AU": ("en-AU", "en"),
    "en-CA": ("en-CA", "en"),
    "en-IN": ("en-IN", "en"),
    "en-NZ": ("en-NZ", "en"),
    "en-SG": ("en-SG", "en"),
    "es": ("es-ES", "es"),
    "es-ES": ("es-ES", "es"),
    "es-MX": ("es-MX", "es"),
    "fr": ("fr-FR", "fr"),
    "fr-FR": ("fr-FR", "fr"),
    "id": ("id-ID", "id"),
    "id-ID": ("id-ID", "id"),
    "it": ("it-IT", "it"),
    "it-IT": ("it-IT", "it"),
    "ja": ("ja-JP", "ja"),
    "ja-JP": ("ja-JP", "ja"),
    "ko": ("ko-KR", "ko"),
    "ko-KR": ("ko-KR", "ko"),
    "nl": ("nl-NL", "nl"),
    "nl-NL": ("nl-NL", "nl"),
    "pt": ("pt-PT", "pt"),
    "pt-PT": ("pt-PT", "pt"),
    "pt-BR": ("pt-BR", "pt-BR"),
    "th": ("th-TH", "th"),
    "th-TH": ("th-TH", "th"),
    "tr": ("tr-TR", "tr"),
    "tr-TR": ("tr-TR", "tr"),
    "vi": ("vi-VN", "vi"),
    "vi-VN": ("vi-VN", "vi"),
    "zh": ("zh-CN", "zh"),
    "zh-CN": ("zh-CN", "zh-CN"),
    "zh-TW": ("zh-TW", "zh-TW"),
}

# 出口国家 → 浏览器/请求主时区（检测源无 timezone 时回填；与 IP 国家对齐）
COUNTRY_TIMEZONE = {
    "AU": "Australia/Sydney",
    "BR": "America/Sao_Paulo",
    "CA": "America/Toronto",
    "DE": "Europe/Berlin",
    "ES": "Europe/Madrid",
    "FR": "Europe/Paris",
    "GB": "Europe/London",
    "ID": "Asia/Jakarta",
    "IN": "Asia/Kolkata",
    "IT": "Europe/Rome",
    "JP": "Asia/Tokyo",
    "KR": "Asia/Seoul",
    "MX": "America/Mexico_City",
    "NL": "Europe/Amsterdam",
    "NZ": "Pacific/Auckland",
    "PT": "Europe/Lisbon",
    "SG": "Asia/Singapore",
    "TH": "Asia/Bangkok",
    "TW": "Asia/Taipei",
    "US": "America/New_York",
    "VN": "Asia/Ho_Chi_Minh",
    "AT": "Europe/Vienna",
    "BE": "Europe/Brussels",
    "CH": "Europe/Zurich",
    "CN": "Asia/Shanghai",
    "HK": "Asia/Hong_Kong",
    "IE": "Europe/Dublin",
    "MY": "Asia/Kuala_Lumpur",
    "PH": "Asia/Manila",
    "PL": "Europe/Warsaw",
    "SE": "Europe/Stockholm",
    "TR": "Europe/Istanbul",
    "AE": "Asia/Dubai",
    "AR": "America/Argentina/Buenos_Aires",
    "CL": "America/Santiago",
    "CO": "America/Bogota",
    "ZA": "Africa/Johannesburg",
}
QUICK_PROXY_COUNTRY_CHOICES = tuple(COUNTRY_TIMEZONE.keys())
QUICK_PROXY_COUNTRY_NAMES = {
    "AU": "澳大利亚", "BR": "巴西", "CA": "加拿大", "DE": "德国",
    "ES": "西班牙", "FR": "法国", "GB": "英国", "ID": "印度尼西亚",
    "IN": "印度", "IT": "意大利", "JP": "日本", "KR": "韩国",
    "MX": "墨西哥", "NL": "荷兰", "NZ": "新西兰", "PT": "葡萄牙",
    "SG": "新加坡", "TH": "泰国", "TW": "中国台湾", "US": "美国",
    "VN": "越南", "AT": "奥地利", "BE": "比利时", "CH": "瑞士",
    "CN": "中国", "HK": "中国香港", "IE": "爱尔兰", "MY": "马来西亚",
    "PH": "菲律宾", "PL": "波兰", "SE": "瑞典", "TR": "土耳其",
    "AE": "阿联酋", "AR": "阿根廷", "CL": "智利", "CO": "哥伦比亚",
    "ZA": "南非",
}
QUICK_PROXY_COUNTRY_ALIASES = {
    "台湾": "TW", "臺灣": "TW", "中国臺灣": "TW",
    "香港": "HK", "中國香港": "HK", "中國": "CN",
    "韩国": "KR", "韓國": "KR", "土耳其共和国": "TR",
    "阿拉伯联合酋长国": "AE", "印尼": "ID",
}
QUICK_PROXY_COUNTRY_DISPLAY_CHOICES = tuple(
    f"{QUICK_PROXY_COUNTRY_NAMES[code]}（{code}）"
    for code in QUICK_PROXY_COUNTRY_CHOICES
)


def quick_proxy_country_display(value: str) -> str:
    code = quick_proxy_country_code(value)
    if not code:
        return str(value or "").strip()
    return f"{QUICK_PROXY_COUNTRY_NAMES.get(code, code)}（{code}）"


def quick_proxy_country_code(value: str) -> str:
    """解析国家代码、中文名称、中文显示项或旧版代理 URL。"""
    text = str(value or "").strip()
    display_match = re.search(r"[（(]\s*([A-Za-z]{2})\s*[）)]\s*$", text)
    if display_match:
        country = display_match.group(1).upper()
        return country if country in QUICK_PROXY_COUNTRY_CHOICES else ""
    if re.fullmatch(r"[A-Za-z]{2}", text):
        country = text.upper()
        return country if country in QUICK_PROXY_COUNTRY_CHOICES else ""
    normalized_name = re.sub(r"\s+", "", text)
    for country, name in QUICK_PROXY_COUNTRY_NAMES.items():
        if normalized_name == re.sub(r"\s+", "", name):
            return country
    alias_country = QUICK_PROXY_COUNTRY_ALIASES.get(normalized_name, "")
    if alias_country:
        return alias_country
    try:
        parsed = urlsplit(text)
        hostname = str(parsed.hostname or "").strip().upper()
        port = parsed.port
    except Exception:
        return ""
    if (
        parsed.scheme.lower() in {"http", "https"}
        and hostname in QUICK_PROXY_COUNTRY_CHOICES
        and port is None
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    ):
        return hostname
    return ""

DEVICE_PROFILES = [
    {"locale": "en-US", "languages": ["en-US", "en"], "timezone": "America/New_York"},
    {"locale": "en-US", "languages": ["en-US", "en"], "timezone": "America/Chicago"},
    {"locale": "en-US", "languages": ["en-US", "en"], "timezone": "America/Los_Angeles"},
    {"locale": "en-GB", "languages": ["en-GB", "en"], "timezone": "Europe/London"},
]
REGISTER_DEVICE_PROFILES = [
    {"locale": "ja-JP", "languages": ["ja-JP", "ja"], "timezone": "Asia/Tokyo"},
]
TEAM_DEVICE_PROFILES = [
    {"locale": "en-US", "languages": ["en-US", "en"], "timezone": "America/New_York"},
    {"locale": "en-US", "languages": ["en-US", "en"], "timezone": "America/Chicago"},
    {"locale": "en-US", "languages": ["en-US", "en"], "timezone": "America/Los_Angeles"},
]
PAYMENT_DEVICE_PROFILES = [
    {"locale": "ja-JP", "languages": ["ja-JP", "ja"], "timezone": "Asia/Tokyo"},
]
COUNTRY_BROWSER_LOCALE = {
    "AU": "en-AU", "BR": "pt-BR", "CA": "en-CA", "DE": "de-DE", "ES": "es-ES",
    "FR": "fr-FR", "GB": "en-GB", "ID": "id-ID", "IN": "en-IN", "IT": "it-IT",
    "JP": "ja-JP", "KR": "ko-KR", "MX": "es-MX", "NL": "nl-NL", "NZ": "en-NZ",
    "PT": "pt-PT", "SG": "en-SG", "TH": "th-TH", "TW": "zh-TW", "US": "en-US",
    "VN": "vi-VN",
    "AT": "de-DE", "BE": "nl-NL", "CH": "de-DE", "CN": "zh-CN", "HK": "zh-TW",
    "IE": "en-GB", "MY": "en-SG", "PH": "en-US", "PL": "en-US", "SE": "en-US",
    "TR": "tr-TR", "AE": "en-US", "AR": "es-MX", "CL": "es-MX", "CO": "es-MX",
    "ZA": "en-US",
}
