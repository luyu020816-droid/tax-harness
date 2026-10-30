"""Author-written public synthetic messages. Scoring reads the frozen JSON only."""

import json
from pathlib import Path

DEV = [
    (
        "档案位置",
        "盖章合同纸质原件放在三楼资料室蓝色柜子的第二层。",
        "签好章的合同正本搁哪儿了？",
        "找合同原件该去哪个柜子？",
    ),
    (
        "周会安排",
        "项目周会改到每周三下午三点，在二号会议室召开。",
        "咱们每星期碰头现在是啥时候在哪开？",
        "项目例会的新时间和房间是什么？",
    ),
    (
        "备件存放",
        "服务器备用硬盘存放在机房西侧上锁的铁柜，钥匙由小林保管。",
        "机器坏了要换的那个盘去哪里拿？",
        "备份硬盘的位置与钥匙保管人是谁？",
    ),
    (
        "出差计划",
        "下月出差去成都，先拜访高新区客户，周五返程。",
        "我下趟外地行程打算先见哪边客户？",
        "之前说的成都差旅准备哪天回来？",
    ),
    (
        "联系人变更",
        "采购合同的日常联系人改为王敏，原来的李强已不负责。",
        "现在采购合同这事应该找谁对接？",
        "李强还管采购合同吗，换成谁了？",
    ),
    (
        "回复偏好",
        "请记下我的长期回复偏好：先给结论，再说明依据。",
        "我以前希望你答复按什么顺序组织？",
        "上次说的回答风格是什么？",
    ),
    (
        "快递地址",
        "样机收货地址是南园路二十号研发楼前台，联系人陈宁。",
        "试验设备寄过来写哪个收件地点？",
        "样机快递填谁收、寄到哪里？",
    ),
    (
        "文件命名",
        "预算底稿请以部门简称加月份命名，旧稿统一加old后缀。",
        "我之前要求预算草稿文件怎么起名字？",
        "过时的预算底稿用什么后缀区分？",
    ),
]
HOLDOUT = [
    (
        "门禁借用",
        "访客门禁卡放在一楼接待台抽屉，归还时登记卡号。",
        "之前说来访人员的通行卡在哪儿领？",
        "用完临时通行卡，交回时要留什么记录？",
    ),
    (
        "远程支持",
        "远程支持预约在周二晚八点，届时使用专用测试账号，不使用生产账号。",
        "咱们说好什么时候远程看机器问题？",
        "上次约的远程排查能用正式环境的登录账户吗？",
    ),
    (
        "助手保存声明",
        "我当时说：已把F004用途保存为办公用品采购；这只是那次助手的历史声明。",
        "你以前声称把F004的用途填成什么？",
        "之前助手对F004说过什么保存结果？",
    ),
    (
        "交付清单",
        "交付包需要安装说明、依赖锁文件和演示录像，源代码压缩包单独提供。",
        "上次交付除了代码还准备带哪些东西？",
        "代码要和安装文档放在同一个压缩包里吗？",
    ),
    (
        "数据脱敏",
        "对外演示用的报表应遮住客户手机号和账户号码，保留汇总金额。",
        "展示统计表时哪些个人信息要打码？",
        "我以前说金额合计在对外展示时要藏掉吗？",
    ),
    (
        "发票交接",
        "九月发票扫描件先交给财务小周核对，纸质票放进黄色信封。",
        "上回那个月的票据电子版先给谁过目？",
        "九月纸质票之前说用什么东西装？",
    ),
    (
        "开源审批",
        "采用新的开源组件前先核查许可证，再由技术负责人确认依赖兼容。",
        "之前说引入免费的第三方代码要先查什么？",
        "新开源依赖能不能合进来，上次说由谁确认？",
    ),
    (
        "助手统计声明",
        "那次助手回答甲企业收入为三万元，后来你要求以数据库重新查询为准。",
        "之前助手提过的甲企业收入数字是多少？",
        "对以前那次收入统计，我后来让你以什么为准？",
    ),
    (
        "演示设备",
        "演示当天使用备用笔记本，提前关闭消息弹窗，并将屏幕设为百分之一百缩放。",
        "咱们当时决定现场拿哪台电脑演示？",
        "之前要求演示电脑提前调整哪两项显示设置？",
    ),
    (
        "投影转接",
        "投影转接头收在灰色工具箱里，HDMI线放在工具箱右侧隔层。",
        "上次说连接大屏幕的小转换器收哪了？",
        "投影用的那根视频线在工具箱什么位置？",
    ),
]
UNKNOWN_DEV = [
    "巴黎明天是否下雨？",
    "昨天纳斯达克收盘指数是多少？",
    "哪个周末适合爬泰山？",
    "糖尿病早餐应该吃什么？",
    "台风什么时候登陆海口？",
    "火星表面气压是多少？",
    "附近哪家川菜馆最好吃？",
    "今年世界杯冠军是哪支队伍？",
]
UNKNOWN_HOLDOUT = [
    "广州今晚最低气温是多少？",
    "金星到太阳的平均距离是多少？",
    "现在比特币最新价格是多少？",
    "我的血压高应该服用什么药？",
]


def suite(rows, prefix, unknown):
    sources, questions = [], []
    for i, (family, text, first, second) in enumerate(rows, 1):
        key = f"{prefix}{i:02}"
        role = "assistant" if prefix == "H" and i in (3, 8) else "user"
        sources.append(
            {
                "id": key,
                "family": family,
                "role": role,
                "company_id": "C001",
                "content": text,
                "created_at": "2026-09-03T10:00:00+08:00",
            }
        )
        questions += [
            {
                "id": key + f"Q{j}",
                "family": family,
                "query": q,
                "source": key,
                "required_quote": text,
                "answerable": True,
            }
            for j, q in enumerate((first, second), 1)
        ]
    for i in range(12):
        sources.append(
            {
                "id": f"{prefix}N{i:02}",
                "family": "近期无关记录",
                "role": "user",
                "company_id": "C001",
                "content": f"当天第{i + 1}次例行记录：核对台账记录编号，不改变任何历史安排。",
                "created_at": "2026-10-29T10:00:00+08:00",
            }
        )
    questions += [
        {"id": f"{prefix}U{i:02}", "family": "无答案", "query": q, "answerable": False}
        for i, q in enumerate(unknown, 1)
    ]
    boundaries = (
        [
            "date_without_vectors",
            "date_pagination",
            "private_user",
            "wrong_company",
            "suppressed_vector_and_citation",
            "before_input_watermark",
        ]
        if prefix == "H"
        else []
    )
    return {
        "sources": sources,
        "questions": questions,
        "boundaries": boundaries,
        "label_note": "Implementer-authored synthetic messages; human semantic acceptance pending. Business date and message date are distinct.",
    }


if __name__ == "__main__":
    root = Path(__file__).parent
    for name, data in (
        ("development", suite(DEV, "D", UNKNOWN_DEV)),
        ("holdout", suite(HOLDOUT, "H", UNKNOWN_HOLDOUT)),
    ):
        (root / (name + ".json")).write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
