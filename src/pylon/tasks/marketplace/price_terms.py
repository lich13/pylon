"""Keep payment roles and units next to one item's amount, without conversion."""
import re


def extract(text, analysis):
    evidence=analysis.evidence or ''
    currency='USD' if re.search(r'美元|\bUSD\b|\$',evidence,re.I) else 'USDT' if re.search(r'USDT|(?<=\d)\s*U\b',evidence,re.I) else 'CNY'
    unit=''
    period=re.search(r'(?:元|块|美元|USD|USDT|U|\d)\s*/\s*(\d*\s*(?:年|季度|季|个月|月|周|天|日|小时|人|席))',evidence,re.I)
    if period:unit=period[1].strip()
    else:
        period=re.search(r'(年付|季付|月付|周付|日付|每月|每天|每年)',evidence)
        if period:unit={'年付':'年','季付':'季','月付':'月','周付':'周','日付':'天','每月':'月','每天':'天','每年':'年'}[period[1]]
    roles={'deposit':[],'shipping':[],'reference':[]}
    for candidate in analysis.candidates:
        if candidate.reason in {'reference','reference_or_expense'}:
            roles['reference'].append({'amount':candidate.amount,'evidence':candidate.evidence})
        elif candidate.reason=='expense':
            if re.search(r'定金|订金|押金',candidate.evidence):key='deposit'
            elif re.search(r'运费|邮费',candidate.evidence):key='shipping'
            else:continue
            roles[key].append({'amount':candidate.amount,'evidence':candidate.evidence})
    return {'currency':currency,'billing_unit':unit,'price_roles':roles}
