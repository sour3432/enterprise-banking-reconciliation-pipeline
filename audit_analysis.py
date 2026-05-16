import pandas as pd
import numpy as np

df = pd.read_csv('data/silver/enterprise_banking_messy_dataset_15324f25_f3c6_4628_9bab_575f59a2eafa__7ed86132-b864-4562-8a47-5608f4fa57fc.csv', dtype=str, keep_default_na=False)

print('=' * 80)
print('VALIDATION CALIBRATION AUDIT - ROOT CAUSE ANALYSIS')
print('=' * 80)

# Check what the validation engine expects vs what exists
fm = {
    'transaction_id': 'txn_id',
    'transaction_date': 'txn_date_parsed',
    'transaction_date_raw_fallback': 'txn_date',
    'transaction_amount': 'messy_amount_amount_numeric',
    'transaction_amount_fallback': 'amount_inr_amount_numeric',
    'currency_code_primary': 'messy_amount_currency_code',
    'currency_code_secondary': 'currency_code',
    'account_id': 'primary_account_number_standardized',
    'account_id_fallback': 'primary_account_number'
}

print('\n1. MANDATORY FIELD ANALYSIS:\n')

# Transaction ID
print('   TRANSACTION_ID (S5 FATAL if missing):')
tid_col = fm['transaction_id']
tid_std_col = 'txn_id_standardized'
if tid_col in df.columns:
    null_pct = 100 * (df[tid_col].fillna('').astype(str).str.strip() == '').sum() / len(df)
    print(f'     Expected column "{tid_col}": {null_pct:.1f}% NULL >> {null_pct:.0f}% REJECTED ❌')
if tid_std_col in df.columns:
    null_pct = 100 * (df[tid_std_col].fillna('').astype(str).str.strip() == '').sum() / len(df)
    print(f'     Available column "{tid_std_col}": {null_pct:.1f}% NULL >> PROPER STANDARDIZED COLUMN ✓')

# Transaction Date  
print('\n   TRANSACTION_DATE (S5 FATAL if missing):')
tdate_col = fm['transaction_date']
tfb_col = fm['transaction_date_raw_fallback']
if tdate_col in df.columns:
    null_pct = 100 * (df[tdate_col].fillna('').astype(str).str.strip() == '').sum() / len(df)
    print(f'     Expected column "{tdate_col}": {null_pct:.1f}% NULL >> {null_pct:.0f}% REJECTED ❌')
if tfb_col in df.columns:
    null_pct = 100 * (df[tfb_col].fillna('').astype(str).str.strip() == '').sum() / len(df)
    print(f'     Fallback column "{tfb_col}": {null_pct:.1f}% NULL >> ALSO FAILS ❌')

# Check what date columns have good data
print('\n   Available date columns (checking what should be used):')
for col in ['messy_txn_date', 'messy_txn_date_parsed', 'trade_date_parsed', 'value_date_parsed']:
    if col in df.columns:
        null_pct = 100 * (df[col].fillna('').astype(str).str.strip() == '').sum() / len(df)
        non_empty = df[col].fillna('').astype(str).str.strip() != ''
        if non_empty.any():
            sample = df[col][non_empty].iloc[0]
        else:
            sample = 'N/A'
        if null_pct < 50:
            print(f'     {col}: {null_pct:.1f}% NULL ✓ VIABLE | Sample: {sample}')
        else:
            print(f'     {col}: {null_pct:.1f}% NULL ❌')

# Currency Code
print('\n   CURRENCY_CODE (S5 FATAL if missing):')
ccprim = fm['currency_code_primary']
ccsec = fm['currency_code_secondary']
if ccprim in df.columns:
    null_pct = 100 * (df[ccprim].fillna('').astype(str).str.strip() == '').sum() / len(df)
    print(f'     Primary column "{ccprim}": {null_pct:.1f}% NULL')

if ccsec in df.columns:
    null_pct = 100 * (df[ccsec].fillna('').astype(str).str.strip() == '').sum() / len(df)
    print(f'     Secondary column "{ccsec}": {null_pct:.1f}% NULL')
    vals = df[ccsec].fillna('').astype(str).str.strip()
    non_empty_pct = 100 * (vals != '').sum() / len(df)
    print(f'     >> {non_empty_pct:.1f}% has currency values')

# Amount
print('\n   TRANSACTION_AMOUNT (S5 FATAL if missing/unparsed):')
amt_col = fm['transaction_amount']
if amt_col in df.columns:
    null_pct = 100 * (df[amt_col].fillna('').astype(str).str.strip() == '').sum() / len(df)
    parse_fail_col = 'messy_amount_amount_parse_failed'
    parse_fail = 0
    if parse_fail_col in df.columns:
        parse_fail = 100 * (df[parse_fail_col].astype(str).str.lower().isin({'true', '1', 'yes'}).sum()) / len(df)
    print(f'     Column "{amt_col}": {null_pct:.1f}% NULL, {parse_fail:.1f}% parse failures')
    print(f'     >> Total failure rate: {max(null_pct, parse_fail):.1f}%')

# Account ID
print('\n   ACCOUNT_ID (S5 FATAL if missing):')
acct_col = fm['account_id']
acct_fb = fm['account_id_fallback']
if acct_col in df.columns:
    null_pct = 100 * (df[acct_col].fillna('').astype(str).str.strip() == '').sum() / len(df)
    print(f'     Primary column "{acct_col}": {null_pct:.1f}% NULL')
if acct_fb in df.columns:
    null_pct = 100 * (df[acct_fb].fillna('').astype(str).str.strip() == '').sum() / len(df)
    print(f'     Fallback column "{acct_fb}": {null_pct:.1f}% NULL')

print('\n' + '=' * 80)
print('ESTIMATED S5 REJECTION RATE (mandatory failures):')
print('=' * 80)

# Simulate actual validation logic
tid_mandatory = 100 * (df['txn_id'].fillna('').astype(str).str.strip() == '').sum() / len(df)
tdate_mandatory = 100 * ((df['txn_date_parsed'].fillna('').astype(str).str.strip() == '') & 
                          (df['txn_date'].fillna('').astype(str).str.strip() == '')).sum() / len(df)
amt_mandatory = 100 * (pd.to_numeric(df['messy_amount_amount_numeric'], errors='coerce').isna()).sum() / len(df)
cc_mandatory = 100 * ((df['messy_amount_currency_code'].fillna('').astype(str).str.strip() == '') &
                       (df['currency_code'].fillna('').astype(str).str.strip() == '')).sum() / len(df)
acct_mandatory = 100 * ((df['primary_account_number_standardized'].fillna('').astype(str).str.strip() == '') &
                         (df['primary_account_number'].fillna('').astype(str).str.strip() == '')).sum() / len(df)

print(f'\ntxn_id mandatory failure: {tid_mandatory:.1f}%')
print(f'txn_date mandatory failure: {tdate_mandatory:.1f}%')
print(f'amount mandatory failure: {amt_mandatory:.1f}%')
print(f'currency_code mandatory failure: {cc_mandatory:.1f}%')
print(f'account_id mandatory failure: {acct_mandatory:.1f}%')

# Union (any of these would cause S5 rejection)
has_any_mandatory = (
    (df['txn_id'].fillna('').astype(str).str.strip() == '') |
    ((df['txn_date_parsed'].fillna('').astype(str).str.strip() == '') & 
     (df['txn_date'].fillna('').astype(str).str.strip() == '')) |
    (pd.to_numeric(df['messy_amount_amount_numeric'], errors='coerce').isna()) |
    ((df['messy_amount_currency_code'].fillna('').astype(str).str.strip() == '') &
     (df['currency_code'].fillna('').astype(str).str.strip() == '')) |
    ((df['primary_account_number_standardized'].fillna('').astype(str).str.strip() == '') &
     (df['primary_account_number'].fillna('').astype(str).str.strip() == ''))
)

print(f'\nTotal records with ANY mandatory field missing: {has_any_mandatory.sum():,} ({100*has_any_mandatory.sum()/len(df):.1f}%)')

print('\n' + '=' * 80)
print('CONCLUSION: PRIMARY ISSUES')
print('=' * 80)
print(f'''
1. FIELD MAPPING MISMATCH (CRITICAL)
   - txn_id column is 63.2% empty, but txn_id_standardized is 0% empty
   - txn_date and txn_date_parsed are both 40.6% empty
   - Validation rules point to RAW columns instead of STANDARDIZED columns
   
2. DATE PARSING FAILURE (MAJOR)
   - ~40.6% of date parsing is failing in standardization layer
   - No proper fallback date fields being used by validation
   
3. CURRENCY CODE SECONDARY FALLBACK (MODERATE)
   - Primary currency_code has ~16.8% nullness
   - Secondary fallback 'currency_code' only provides coverage for some
   
This explains the ~69.3% (35,362/51,003) S5 rejection rate.
The solution is to update field_mapping in validation_rules.yaml.
''')
