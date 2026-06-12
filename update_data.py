#!/usr/bin/env python3
"""
D360 Dashboard Data Updater
Run via Claude Unleashed cron — uses MCP Org62 to pull entitlement data
and generates data.json + daily/*.json for the GitHub Pages dashboard.
"""
import json
import sys
import os
from datetime import date, datetime
from collections import defaultdict
from pathlib import Path

# ── CONFIG ────────────────────────────────────────────────────────────
OUTPUT_DIR = Path(__file__).parent
DAILY_DIR = OUTPUT_DIR / "daily"
DAILY_DIR.mkdir(exist_ok=True)

# Account IDs from SpecialistForecast (Julián's D360 territory)
# This list is updated automatically by the CU skill
ACCOUNT_IDS = [
    "0013000000rpjd2AAA",  # OSDE
    "00130000005dLuMAAU",  # MercadoLibre
    "00130000005tQ9vAAE",  # Telecom Argentina
    "0013000001CCpybAAD",  # Banco Macro
    "0013000000IkqzpAAB",  # Banco Hipotecario
    "0013y00001c7SoQAAU",  # Andina ART
    "0010M00001QqhevQAB",  # BGH Tech Partner
    "0010M00001S9LQSQA3",  # Banco Consorcio
    "0013000000rpk4cAAA",  # abc S.A.
    "0013000000hK3LeAAK",  # Abastible
    "0013000000rosvvAAA",  # Banco Ripley
    "0013000000NRQ0oAAH",  # Bice Vida
    "0013000001GXXyUAAX",  # Bidcom SRL
    "0010M00001YttHgQAJ",  # Blue Express
    "0013000000IknUpAAJ",  # Banco Del Estado de Chile
    "0013000000KxNseAAF",  # LATAM Airlines
    "0013000001C9skXAAR",  # Falabella Tecnologia
    "0010M00001WaxcVQAR",  # Aeropuertos Argentina
    "0013000000I3Yo2AAF",  # Banco BBVA Argentina
    "0013000000FIu1bAAD",  # Aerolineas Argentinas
]

# Parent → children grouping for display (child IDs → parent display ID)
ACCOUNT_GROUPS = {
    # Telecom group — parent: Telecom Argentina S.A.
    '0013000000Boo13AAB': '00130000005tQ9vAAE',   # Telecom S.A.
    '0010M00001SJ7JnQAL': '00130000005tQ9vAAE',   # Telecom S.A. - Copado
    '0013y00001eDTFKAA4': '00130000005tQ9vAAE',   # Telecom Argentina S.A. (dup)
    '001ed00000GPtRaAAL': '00130000005tQ9vAAE',   # Telecom Argentina S.A. (dup)
    '001ed00000GR5gkAAD': '00130000005tQ9vAAE',   # Telecom Argentina S.A. (dup)
    '0013y00001cQE4pAAG': '00130000005tQ9vAAE',   # Micro Sistemas S.A.U.
    # MercadoLibre group — parent: MercadoLibre SRL
    '001ed00000ZkW7BAAV': '00130000005dLuMAAU',   # Mercado Libre SELA TAB
    # YPF group — parent: YPF S.A 2
    '0010M00001UV2V8QAL': '0010M00001YtwTbQAJ',   # YPF
    '0013000000I3YnKAAV': '0010M00001YtwTbQAJ',   # YPF S.A.
    # Pan American Energy group — parent: PAE Sucursal Argentina
    '0013000001CCp8VAAT': '0013000001GXGozAAH',   # PAE Llc Upstream
    # BHN group — parent: BHN Seguros
    '0013000000IkqzpAAB': '00130000016iGuYAAU',   # Banco Hipotecario
}

# This script is meant to be called from the CU skill with data injected
# When run standalone, it reads from stdin (JSON from MCP calls)


def process_entitlement_records(records):
    """Process sfbase__EntitlementSchedule__c records into account dict."""
    accounts = {}
    for r in records:
        ent = r['sfbase__Entitlement__r']
        acc = ent['sfbase__Account__r']
        acc_id = acc['Id']
        if acc_id not in accounts:
            accounts[acc_id] = {
                'id': acc_id,
                'name': acc['Name'],
                'country': acc.get('BillingCountry', '??'),
                'owner': acc.get('Owner', {}).get('Name', 'Unknown') if acc.get('Owner') else 'Unknown',
                'schedules': []
            }
        accounts[acc_id]['schedules'].append({
            'type': ent['sfbase__EntitlementName__c'],
            'start': r.get('sfbase__StartDate__c', ''),
            'end': r.get('sfbase__EndDate__c', ''),
            'allowance': round(r.get('sfbase__Allowance__c', 0) or 0),
            'usage': round(abs(r.get('Usage__c', 0) or 0)),
            'days_elapsed': round(r.get('DaysElapsed__c', 0) or 0),
            'days_remaining': round(r.get('DaysRemaining__c', 0) or 0),
            'overage_date': r.get('EstimatedOverageDate__c', '') or ''
        })
    return accounts


def calculate_metrics(acc):
    """Calculate CRR, upsell, cohort etc from raw account data."""
    schedules = acc['schedules']
    total_allow = sum(s['allowance'] for s in schedules)
    total_usage = sum(s['usage'] for s in schedules)
    main = max(schedules, key=lambda s: s['allowance']) if schedules else {}
    td = main.get('days_elapsed', 0) + main.get('days_remaining', 0)
    pu = total_usage / total_allow if total_allow > 0 else 0
    tp = main.get('days_elapsed', 0) / td if td > 0 else 0
    crr = round((pu / tp) * 100, 1) if tp > 0 else 0
    daily = total_usage / main.get('days_elapsed', 1) if main.get('days_elapsed', 0) > 0 else 0
    proj = daily * td
    upsell = round(max(0, proj - total_allow) * 0.001)
    status = 'growth' if crr > 80 else 'track' if crr > 20 else 'activate'
    crr_cohort = 'Consuming Well' if crr >= 85 else 'Consuming' if crr >= 50 else 'Under Consuming'
    return {
        **acc,
        'allowance': total_allow,
        'usage': total_usage,
        'crr': min(crr, 500),
        'pct_used': round(pu * 100, 1),
        'pct_remaining': round(max(0, (1 - pu)) * 100, 1),
        'in_overage': total_usage > total_allow,
        'days_remaining': main.get('days_remaining', 0),
        'days_elapsed': main.get('days_elapsed', 0),
        'overage_date': main.get('overage_date', ''),
        'upsell_usd': upsell,
        'status': status,
        'crr_cohort': crr_cohort,
        'daily_rate': round(daily),
        'sela': any(s['allowance'] > 1000000 for s in schedules)
    }


def process_daily_records(records):
    """Process EntitlementTransaction records into per-account daily series."""
    by_acc = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))
    for r in records:
        try:
            ent = r['sfbase__EntitlementSchedule__r']['sfbase__Entitlement__r']
            acc_id = ent['sfbase__Account__r']['Id']
            ent_name = ent['sfbase__EntitlementName__c']
            date_str = r['sfbase__TransactionDate__c']
            qty = abs(r.get('sfbase__Quantity__c', 0) or 0)
            # Classify type
            if 'Data Services' in ent_name:
                t = 'DSC'
            elif 'Flex' in ent_name:
                t = 'Flex'
            elif 'Super Messages' in ent_name:
                t = 'SuperMsg'
            elif 'Named Profiles' in ent_name:
                t = 'Profiles'
            elif 'Corporate' in ent_name:
                t = 'CorporateContacts'
            elif 'Storage' in ent_name:
                t = 'Storage'
            elif 'Message' in ent_name or 'WhatsApp' in ent_name:
                t = 'Messages'
            elif 'Commerce' in ent_name or 'B2C' in ent_name:
                t = 'Commerce'
            elif 'Enterprise Edition' in ent_name:
                t = 'EnterpriseContacts'
            else:
                t = 'Other'
            by_acc[acc_id][date_str][t] += qty
        except (KeyError, TypeError):
            continue
    return by_acc


def calc_avg(dates_dict, all_dates, n_days):
    """Average daily usage over the last n_days."""
    window = all_dates[-n_days:] if len(all_dates) >= n_days else all_dates
    if not window:
        return 0
    total = sum(sum(dates_dict[d].values()) for d in window)
    return round(total / len(window))


def write_daily_files(by_acc):
    """Write daily/ACCOUNT_ID.json files."""
    for acc_id, dates in by_acc.items():
        all_dates = sorted(dates.keys())
        all_types = sorted(set(
            t for d in dates.values() for t in d
            if sum(dates[dd].get(t, 0) for dd in all_dates) > 100
        ))
        series = {t: [round(dates[d].get(t, 0)) for d in all_dates] for t in all_types}

        # Trends: avg daily usage over 30/90/120 days
        avg30 = calc_avg(dates, all_dates, 30)
        avg90 = calc_avg(dates, all_dates, 90)
        avg120 = calc_avg(dates, all_dates, 120)
        # DSC vs Flex breakdown per window
        def avg_by_type(n):
            window = all_dates[-n:] if len(all_dates) >= n else all_dates
            if not window:
                return {}
            out = {}
            for t in ['DSC', 'Flex']:
                total = sum(dates[d].get(t, 0) for d in window)
                out[t] = round(total / len(window))
            return out

        out = {
            'dates': [d[5:] for d in all_dates],  # MM-DD format
            'series': series,
            'trends': {
                'd30': avg30, 'd90': avg90, 'd120': avg120,
                'd30_by_type': avg_by_type(30),
                'd90_by_type': avg_by_type(90),
                'd120_by_type': avg_by_type(120),
            }
        }
        with open(DAILY_DIR / f"{acc_id}.json", 'w') as f:
            json.dump(out, f)
        print(f"  Daily: {acc_id} ({len(all_dates)}d, {list(all_types)[:3]})")


def main():
    """Main entry point — reads JSON data from stdin or file arg."""
    if len(sys.argv) > 1:
        # Two files: entitlements.json and transactions.json
        with open(sys.argv[1]) as f:
            ent_data = json.load(f)
        tx_data = None
        if len(sys.argv) > 2:
            with open(sys.argv[2]) as f:
                tx_data = json.load(f)
    else:
        data = json.load(sys.stdin)
        ent_data = data.get('entitlements', data)
        tx_data = data.get('transactions')

    # Process entitlements
    print("Processing entitlement records...")
    accounts = process_entitlement_records(ent_data.get('records', []))
    result = [calculate_metrics(acc) for acc in accounts.values()]

    # Apply grouping: add group_id to each account
    for acc in result:
        acc['group_id'] = ACCOUNT_GROUPS.get(acc['id'], acc['id'])

    result.sort(key=lambda x: -x['crr'])
    print(f"  {len(result)} accounts processed")

    # Write data.json
    output = {
        'updated': str(date.today()),
        'generated_at': datetime.now().isoformat(),
        'accounts': result
    }
    with open(OUTPUT_DIR / 'data.json', 'w') as f:
        json.dump(output, f, indent=2)
    print(f"Written data.json ({len(result)} accounts)")

    # Process daily transactions
    if tx_data:
        print("Processing daily transactions...")
        by_acc = process_daily_records(tx_data.get('records', []))
        write_daily_files(by_acc)

    print("Done.")


if __name__ == '__main__':
    main()
