"""Known page furniture is not authored trading evidence."""
import re


def contamination(source, title, body):
    if source!='v2ex':return ''
    if 'hub-post-body' in body:return '平台侧栏内容混入正文'
    # Old captures were flattened to text, losing the DOM identity. Require
    # several distinct phrases from this one sidebar module, not a lone
    # mention of an agent, a wallet or a skill.
    markers=('exe-hub','skill.md','ed25519','Solana','$V2EX')
    if sum(word.casefold() in body.casefold() for word in markers)>=4 and not re.search(r'exe-hub|\$V2EX',title,re.I):
        return '旧记录正文来自平台 Agent 侧栏，需重新读取原帖'
    return ''
