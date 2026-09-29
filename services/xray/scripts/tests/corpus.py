# -*- coding: utf-8 -*-
"""
测试共用的分享链接语料和 config.json 模板。

fixtures/cases.json: 每个用例的分享链接，以及当前代码预期的 parse_share_url 结果和 to_xray_outbound 结果
(outbound 的键顺序也是输出的一部分)，或预期的 ValueError 文字。
每个用例都与重构前 (f71d24c) 的代码对同一链接的输出逐个核对过，差异只有下面列出的有意修复 (E3)。
不要从代码重新生成，行为有意改变时手动修改 (并更新下面的列表)。
重构前的输出可以重新得到: 用 git show f71d24c:services/xray/scripts/XrayConfigHandler.py 取出旧代码，
对 cases.json 中的链接运行 parse_share_url / to_xray_outbound。
note 以 "E3:" 开头的用例是为这些修复添加的。

有意修复 (E3)，与 f71d24c 相比 (括号中是体现差异的用例):
 1. vmess 用户 level 固定为 0: 分享链接里的 "v" 是链接格式版本，不是 level
    (所有带 "v": "2" 的 vmess 用例，以前 level 为 2)。
 2. trojan/vless 的 userinfo 做 percent-decode 并完整保留，不再在未编码的 ':' 处截断
    (trojan_pct_password, trojan_colon_password, vless_pct_id)。
 3. outbound tag 与保留 tag (api/DIRECT/BLACKHOLE) 相同时加 "-proxy" 后缀
    (vmess_remark_api, vless_remark_direct, trojan_remark_blackhole, ss_remark_direct)。
 4. 查询参数不能覆盖核心字段 (address、port、id/password、method、protocol、remark)
    (vless_query_override: 以前 ?protocol=vmess 会输出 vmess outbound; trojan_query_override, ss_query_override)。
 5. 旧格式 ss://base64(...) 按最后一个 '@' 拆分，密码可以含 '@' (ss_legacy_at_password)。
 6. 支持 SIP002 明文 (percent-encoded) userinfo，如 SS-2022 (ss_sip002_plain_2022, ss_sip002_plain_colon_pw)。
 7. base64 userinfo 的 '=' 被编码为 %3D 时也能解码 (ss_sip002_pct_padding)。
 8. vmess 的 "v" 不是数字时不再崩溃 (vmess_v_nonnumeric)。
 9. 端口缺失、非数字、为 0 或超出 1-65535 时抛出明确的 ValueError ("缺少端口" / "端口无效")
    (所有带 error 的用例；vless_port_zero、vmess_missing_port、ss_invalid_port 以前静默生成 port 0 / 70000，
    其余以前抛出 int()、urllib 等的原始异常)。
10. create_xray_service.py 不再有自己的 config.json 模板，与 vless_to_config.py 共用 build_xray_config
    (两者的输出都不变)；vless_to_config.py 新增 --stats-api。
build_xray_config 本身不检查、不修改 outbound: 上面的 tag、端口、level 规则都在 to_xray_outbound 中。

fixtures/create_xray_service_config.json: create_xray_service.py 为 vless_reality_vision 写入的 config.json
(统计 API、日志路径)；fixtures/vless_to_config_default.json: vless_to_config.py 对同一链接的默认输出。
其他用例的预期配置 = 把模板的 outbounds[0] 换成该用例的 outbound，其余部分逐字节相同。
"""

import json
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = TESTS_DIR.parent
FIXTURES_DIR = TESTS_DIR / "fixtures"

# create_xray_service.py 写入的 config.json (include_stats_api=True, loglevel none, /var/log/xray 日志)
CREATE_SERVICE_TEMPLATE = "create_xray_service_config.json"
# vless_to_config.py 默认参数的输出 (即 build_xray_config 的默认值)，末尾有换行
VLESS_TO_CONFIG_TEMPLATE = "vless_to_config_default.json"


def load_cases():
    with open(FIXTURES_DIR / "cases.json", encoding="utf-8") as f:
        return json.load(f)["cases"]


def case_url(name):
    return next(case['url'] for case in load_cases() if case['name'] == name)


def read_fixture(name):
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return f.read()


def dumps_config(config):
    """与 create_xray_service.py / vless_to_config.py 相同的序列化 (不排序键)"""
    return json.dumps(config, indent=4, ensure_ascii=False)


def expected_config(template, outbound, log=None):
    """
    把模板的 outbounds[0] 换成 outbound (log 不为 None 时也替换 log)，返回序列化后的文本 (末尾没有换行)。
    模板本身就是 dumps_config 的输出 (见 TemplateTest.test_templates_are_canonical)，
    所以其余部分与模板逐字节相同。
    """
    config = json.loads(read_fixture(template))
    config["outbounds"][0] = outbound
    if log is not None:
        config["log"] = log
    return dumps_config(config)
