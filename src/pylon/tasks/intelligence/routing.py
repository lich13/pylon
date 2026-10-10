"""Use only independently cited passages after a trade has been routed."""
from pylon.tasks.marketplace.control import digest


def residual(snapshot, extracted):
    refs = {row['ref']: row for row in snapshot['evidence']}
    rows = []
    for segment in extracted.get('intel_segments', []):
        row = refs.get(segment['ref'])
        quote = segment['quote']
        if not row or not quote or quote not in row['body']:
            continue
        # The same price/conditions cannot create a second public notification.
        trade_quotes = [proof['quote'] for offer in extracted.get('offers', [])
                        for key, proof in offer.items()
                        if key in {'price', 'term', 'restrictions', 'allocation_basis'}
                        and isinstance(proof, dict) and proof.get('ref') == row['ref'] and proof.get('quote')]
        if any(proof in quote or quote in proof for proof in trade_quotes):
            continue
        rows.append({**row, 'title': '', 'body': quote, 'complete': False})
    return {**snapshot, 'title': '', 'evidence': rows,
            'fingerprint': digest([(r['ref'], r['body']) for r in rows])}
