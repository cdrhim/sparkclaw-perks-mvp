import os

os.environ['DATABASE_URL'] = 'sqlite:///smoke_test.db'
os.environ['ADMIN_EMAIL'] = 'admin@sparkclaw.co.kr'
os.environ['ADMIN_PASSWORD'] = 'change-me'
os.environ['AUTO_NOTIFY_ON_VERIFIED_REQUEST'] = 'false'

from app import app, db, PortfolioCompany, PerkRequest

app.config['TESTING'] = True

with app.app_context():
    db.drop_all()
    db.create_all()
    db.session.add(PortfolioCompany(name='SparkClaw Portfolio Company', website='https://example.com', allowed_domains='portfolio.com'))
    db.session.commit()

client = app.test_client()

resp = client.post('/request', data={
    'requester_name': 'Alex Kim',
    'requester_email': 'alex@portfolio.com',
    'company_name': 'SparkClaw Portfolio Company',
    'company_website': 'https://portfolio.com',
    'perk_type': 'Supabase credits',
    'use_case': 'We need database, auth, and storage credits for our AI product.',
    'expected_monthly_spend': '$200-$500',
    'notes': 'Priority founder support please.'
}, follow_redirects=True)
assert resp.status_code == 200
assert b'Request #1 submitted successfully.' in resp.data

with app.app_context():
    row = PerkRequest.query.first()
    assert row is not None
    assert row.portfolio_verified is True
    assert row.status == 'portfolio_verified'

login = client.post('/admin/login', data={'email': 'admin@sparkclaw.co.kr', 'password': 'change-me'}, follow_redirects=True)
assert login.status_code == 200
assert b'Admin dashboard' in login.data

approve = client.post('/admin/request/1/action', data={'action': 'approve'}, follow_redirects=True)
assert approve.status_code == 200
assert b'Request #1 approved.' in approve.data

export_resp = client.get('/admin/export.csv')
assert export_resp.status_code == 200
assert b'SparkClaw Portfolio Company' in export_resp.data

print('SMOKE_TEST_OK')
