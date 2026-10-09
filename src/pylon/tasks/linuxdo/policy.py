from __future__ import annotations

import json
from pathlib import Path

from pylon.shared import paths
from .common import digest

GROUPS = {'A': '顶级 AI 资源', 'B': 'AI 编程工具', 'C': '开发与自动化',
          'D': '运维与自托管', 'E': '网络与可靠性', 'F': '效率软件与设备',
          'G': '生活权益', 'H': '资源偏好'}
CATEGORIES = dict(zip(
    ['A1', 'A2', 'A3', 'A4', 'B1', 'B2', 'B3', 'B4', 'B5',
     'C1', 'C2', 'C3', 'C4', 'D1', 'D2', 'D3', 'D4', 'D5',
     'E1', 'E2', 'E3', 'E4', 'F1', 'F2', 'F3', 'F4', 'G1', 'G2', 'G3',
     'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'H7', 'H8'],
    ['免费 API / 公益接入', '订阅优惠与教育权益', '额度补货与开放名额', '模型能力与成本变化',
     'Codex', 'Claude Code', 'Grok Build', 'MCP / Skills / 插件 / Agent 工作流', 'API 兼容与网关工具',
     '实用开源项目', '脚本与自动化', '测试与构建 / CI/CD', '性能优化与降本',
     'Linux / Docker / 服务部署', 'NAS / 存储 / 备份', 'SSH / 远程访问 / 组网', '监控与定时通知', '数据迁移与恢复',
     'DNS / CDN / 证书', '代理路由与连接稳定性', '登录与支付订阅方法', '安全兼容与故障资讯',
     '桌面效率工具', '文件同步与文档工具', '手机路由器硬件技巧', '数码促销与二手',
     '全国实用福利', '北京本地福利', '普通软件会员优惠',
     '非目标模型纯福利', '云主机促销', '影音会员与 PT', '游戏与设计素材', '论文与学术资料',
     '邮箱账号赠送', '非北京限定福利', 'WPS 福利'], strict=True))
CONFIG_FILE = paths.PROFILE_ROOT / 'config/linuxdo_policy.json'


def default_policy() -> dict:
    return {'version': 1, 'groups': ['A', 'B', 'C', 'D'],
            'categories': ['E2', 'E3', 'G1', 'G2', 'H2'],
            'excluded_categories': ['E1', 'E4', 'F1', 'F2', 'F3', 'F4', 'G3',
                                    'H1', 'H3', 'H4', 'H5', 'H6', 'H7', 'H8'],
            'cloud_providers': ['tencent', 'alibaba'], 'cloud_regions': 'all'}


def validate(value: dict) -> dict:
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('unsupported Linux.do policy version')
    allowed = set(default_policy())
    if set(value) - allowed:
        raise ValueError('unknown Linux.do policy fields')
    result = dict(value)
    for key, choices in [('groups', GROUPS), ('categories', CATEGORIES),
                         ('excluded_categories', CATEGORIES), ('cloud_providers', {'tencent', 'alibaba'})]:
        items = value.get(key)
        if not isinstance(items, list) or any(not isinstance(item, str) or item not in choices for item in items):
            raise ValueError('invalid Linux.do policy ' + key)
        result[key] = sorted(set(items))
    if value.get('cloud_regions') not in {'all', 'mainland'}:
        raise ValueError('invalid cloud region policy')
    return result


def load(path: Path | None = None) -> dict:
    path = path or CONFIG_FILE
    return validate(json.loads(path.read_text()) if path.exists() else default_policy())


def allowed_categories(config: dict) -> set[str]:
    config = validate(config)
    return ({code for code in CATEGORIES if code[0] in config['groups']} | set(config['categories'])) - set(config['excluded_categories'])


def fingerprint(config: dict) -> str:
    return digest(validate(config))
