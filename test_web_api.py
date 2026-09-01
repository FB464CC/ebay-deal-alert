"""Small Node-backed regression tests for security-critical web API helpers."""

import base64
import hashlib
import json
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TELEGRAM_MODULE = (ROOT / "web" / "api" / "telegram.js").as_posix()
URL_UTILS_MODULE = (ROOT / "chrome-extension" / "url-utils.js").as_posix()
SCOUT_MODULE = (ROOT / "web" / "api" / "scout-ingest.js").as_posix()
CONFIG_MODULE = (ROOT / "web" / "api" / "config.js").as_posix()
LEDGER_MODULE = (ROOT / "web" / "api" / "ledger.js").as_posix()
DELETION_MODULE = (ROOT / "web" / "api" / "ebay-account-deletion.js").as_posix()
INDEX_HTML = ROOT / "web" / "index.html"


def run_node(expression):
    # Piped via stdin (node -), NOT passed as a `-e` command-line argument.
    # A script containing json.dumps()-escaped quotes/backslashes (real
    # test inputs do: HTML entities, embedded double quotes) round-trips
    # through Windows' CreateProcess argv quoting differently than POSIX
    # shells expect, and node's own argv parser can end up seeing a
    # mangled string - the exact same function called the same way via
    # stdin works correctly. Mirrors run_node_script() below.
    script = (
        f"const t=require({json.dumps(TELEGRAM_MODULE)})._test;"
        f"Promise.resolve({expression}).then(v=>process.stdout.write(JSON.stringify(v)))"
        ".catch(e=>{process.stderr.write(e.message);process.exit(2)})"
    )
    completed = subprocess.run(
        ["node", "-"], cwd=ROOT, input=script, capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def run_node_script(script):
    completed = subprocess.run(
        ["node", "-"],
        cwd=ROOT,
        input=script,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def run_api_handler(module, body, fetch_responses):
    script = f"""
const calls = [];
const responses = {json.dumps(fetch_responses)};
global.fetch = async (url, options = {{}}) => {{
  calls.push({{url, options}});
  const next = responses.shift();
  if (!next) throw new Error('Unexpected fetch call');
  return {{
    status: next.status,
    ok: next.status >= 200 && next.status < 300,
    json: async () => next.body
  }};
}};
process.env.GITHUB_TOKEN = 'test-token';
process.env.GITHUB_REPO = 'owner/repo';
process.env.SETTINGS_PASSWORD = 'test-password';
const handler = require({json.dumps(module)});
const req = {{
  method: 'POST',
  headers: {{'x-settings-password': 'test-password'}},
  body: {json.dumps(body)}
}};
let responseText = '';
const res = {{
  statusCode: 0,
  headers: {{}},
  setHeader(name, value) {{ this.headers[name] = value; }},
  end(value) {{ responseText = Buffer.isBuffer(value) ? value.toString('utf8') : String(value); }}
}};
Promise.resolve(handler(req, res)).then(() => {{
  process.stdout.write(JSON.stringify({{
    status: res.statusCode,
    body: JSON.parse(responseText),
    calls
  }}));
}}).catch(error => {{ process.stderr.write(error.stack); process.exit(2); }});
"""
    return run_node_script(script)


def github_file(records, sha):
    text = "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records)
    return {"sha": sha, "content": base64.b64encode(text.encode()).decode()}


def valid_config():
    return {
        "OWNER_TIMEZONE": "America/New_York",
        "QUIET_HOURS_START": "23:00",
        "QUIET_HOURS_END": "07:00",
        "SAVED_SEARCHES": [
            {
                "id": "outerwear-mens-jacket",
                "category": "outerwear",
                "query": "men's jacket",
                "size": ["M", "L"],
                "max_price": 50,
            }
        ],
        "GRAB_ON_SIGHT_BRANDS": ["Brioni"],
        "STANDARD_BRANDS": ["Brooks Brothers"],
        "PASS_BRANDS": ["Example"],
        "CORPORATE_LOGO_KEYWORDS": ["employee"],
        "CONDITION_HARD_FAIL_KEYWORDS": ["torn"],
        "CONDITION_FLAG_KEYWORDS": ["stain"],
        "FABRIC_GOOD_KEYWORDS": ["cashmere"],
        "GENDER_EXCLUDE_KEYWORDS": ["women"],
        "FABRIC_POLY_KEYWORD": "polyester",
        "PIT_TO_PIT_CAP_INCHES": 30,
    }


def index_javascript(start_marker, end_marker):
    source = INDEX_HTML.read_text(encoding="utf-8")
    start = source.index(start_marker)
    end = source.index(end_marker, start)
    return source[start:end]


class TelegramUrlSafetyTests(unittest.TestCase):
    def test_meta_attributes_keep_apostrophes_inside_double_quotes(self):
        html = (
            '<meta property="og:title" content="Men\'s Zegna Suit 42R">'
            "<meta name='og:description' content='Seller said &quot;it&#x27;s clean&#x2F;ready&quot;'>"
        )
        self.assertEqual(
            run_node(f"t.metas({json.dumps(html)})"),
            [
                ["og:title", "Men's Zegna Suit 42R"],
                ["og:description", 'Seller said "it\'s clean/ready"'],
            ],
        )

    def test_price_prefers_asking_context_then_last_amount(self):
        self.assertEqual(
            run_node("t.textAskingPrice('Retails for $1,200 — asking $180 OBO')"),
            "180",
        )
        self.assertEqual(
            run_node("t.textAskingPrice('Includes a $20 case; total $275')"),
            "275",
        )

    def test_meta_price_wins_and_preserves_non_usd_currency(self):
        meta = [
            ["product:price:amount", "150"],
            ["product:price:currency", "EUR"],
        ]
        expression = (
            f"(()=>{{const p=t.extractListingPrice({json.dumps(meta)},'Retail $1200','asking $180');"
            "return [p,t.formatListingPrice(p)]})()"
        )
        self.assertEqual(
            run_node(expression),
            [{"amount": "150", "currency": "EUR"}, "EUR 150"],
        )

    def test_category_uses_search_query_from_listing_url(self):
        url = "https://www.ebay.com/itm/123?_skw=golf+club+set"
        expression = (
            f"(()=>{{const q=t.searchQueryFromUrl({json.dumps(url)},'Titleist listing {url}');"
            "return [q,t.classify(q),t.classify('Titleist AP1 712 irons'),"
            "t.classify('casino poker chips'),t.classify('shirt with free shipping')]})()"
        )
        self.assertEqual(
            run_node(expression),
            ["golf club set", "golf-equipment", "golf-equipment", "poker-chips", "other"],
        )

    def test_category_falls_back_to_user_text_not_seller_metadata(self):
        url = "https://example.com/listing/123"
        expression = (
            f"(()=>{{const q=t.searchQueryFromUrl({json.dumps(url)},'golf bag {url}');"
            "return [q,t.classify(q)]})()"
        )
        self.assertEqual(run_node(expression), ["golf bag", "golf-equipment"])

    def test_counterfeit_seller_disclosures_hard_fail(self):
        expression = (
            "['replica club heads','my own version of Bottega','handmade tribute watch',"
            "'reproduction casino chips','designer dupe','Rolex homage'].map(x=>"
            "t.counterfeitListingLanguage('Clean title',x))"
        )
        self.assertEqual(run_node(expression), [True] * 6)
        self.assertFalse(
            run_node("t.counterfeitListingLanguage('Vintage genuine watch','Original box included')")
        )

    def test_every_category_prompt_treats_seller_disclosure_as_decisive(self):
        expression = (
            "['golf-equipment','poker-chips','watches','other'].map(c=>"
            "t.buildPrompt(c,'Replica item','my own version handmade tribute','September',"
            "{amount:25,currency:'USD'}).includes('Seller authenticity disclosure is decisive'))"
        )
        self.assertEqual(run_node(expression), [True] * 4)

    def test_private_and_reserved_addresses_are_rejected(self):
        addresses = ["127.0.0.1", "10.2.3.4", "100.64.0.1", "169.254.1.1", "172.16.0.1", "192.168.1.1", "198.51.100.1", "203.0.113.1", "::1", "fc00::1", "fe80::1", "::ffff:127.0.0.1"]
        self.assertEqual(run_node(f"{json.dumps(addresses)}.map(t.isPrivateIp)"), [True] * len(addresses))

    def test_public_addresses_are_allowed(self):
        addresses = ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"]
        self.assertEqual(run_node(f"{json.dumps(addresses)}.map(t.isPrivateIp)"), [False] * len(addresses))

    def test_literal_private_url_and_embedded_credentials_fail_closed(self):
        expression = "Promise.allSettled([t.assertPublicUrl('http://127.0.0.1/x'),t.assertPublicUrl('https://user:pass@example.com/x')]).then(x=>x.map(v=>v.status))"
        self.assertEqual(run_node(expression), ["rejected", "rejected"])

    def test_webhook_secret_comparison_requires_exact_string(self):
        expression = "[t.secretMatches('abc','abc'),t.secretMatches('abc','abd'),t.secretMatches(undefined,undefined)]"
        self.assertEqual(run_node(expression), [True, False, False])


class ExtensionUrlValidationTests(unittest.TestCase):
    def test_urls_are_normalized_and_unsafe_schemes_or_credentials_rejected(self):
        script = (
            f"const u=require({json.dumps(URL_UTILS_MODULE)});"
            "const values=[];"
            "for(const [url,opts] of [['https://example.com/path',{}],['http://example.com',{}],['ftp://example.com',{}],['https://u:p@example.com',{}],['http://example.com',{requireHttps:true}]])"
            "{try{values.push(u.normalizeUrl(url,opts))}catch(e){values.push('rejected')}}"
            "process.stdout.write(JSON.stringify(values))"
        )
        completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(completed.stdout), ["https://example.com/path", "http://example.com/", "rejected", "rejected", "rejected"])


class ScoutIngestValidationTests(unittest.TestCase):
    def test_duplicate_items_are_removed_against_queue_and_request(self):
        script = (
            f"const t=require({json.dumps(SCOUT_MODULE)})._test;"
            "const a={platform:' Facebook ',itemId:' 1 ',title:'A',price:1,itemWebUrl:'https://example.com/1',imageUrl:'',description:''};"
            "const b={...a,title:'duplicate'};const c={...a,itemId:'2'};"
            "const old=[JSON.stringify({...a,platform:'facebook',itemId:'1'})];"
            "process.stdout.write(JSON.stringify(t.withoutDuplicates([a,b,c],old).map(x=>x.itemId.trim())))"
        )
        completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(completed.stdout), ["2"])

    def test_duplicate_items_are_removed_against_durable_seen_keys(self):
        script = (
            f"const t=require({json.dumps(SCOUT_MODULE)})._test;"
            "const a={platform:'facebook',itemId:'1'};const b={...a,itemId:'2'};"
            "const durable=new Set(['facebook:1']);"
            "process.stdout.write(JSON.stringify(t.withoutDuplicates([a,b],[],durable).map(x=>x.itemId)))"
        )
        completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(completed.stdout), ["2"])

    def test_seen_database_lookup_uses_platform_namespaced_item_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "seen.db"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE seen (item_id TEXT PRIMARY KEY, seen_at TEXT)")
            connection.executemany(
                "INSERT INTO seen (item_id, seen_at) VALUES (?, '2026-08-31')",
                [("facebook:1",), ("poshmark:1",)],
            )
            connection.commit()
            connection.close()
            script = (
                f"const t=require({json.dumps(SCOUT_MODULE)})._test;"
                f"const found=[...t.findSeenKeysInDatabase({json.dumps(database_path.as_posix())},"
                "['facebook:1','facebook:2'])].sort();"
                "process.stdout.write(JSON.stringify(found))"
            )
            completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
            self.assertEqual(json.loads(completed.stdout), ["facebook:1"])

    def test_seen_dedup_happens_before_full_queue_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "seen.db"
            connection = sqlite3.connect(database_path)
            connection.execute("CREATE TABLE seen (item_id TEXT PRIMARY KEY, seen_at TEXT)")
            connection.execute(
                "INSERT INTO seen (item_id, seen_at) VALUES ('facebook:1', '2026-08-31')"
            )
            connection.commit()
            connection.close()
            database_content = base64.b64encode(database_path.read_bytes()).decode()
        queue_content = base64.b64encode(("{}\n" * 2000).encode()).decode()
        listing = {
            "platform": "facebook",
            "itemId": "1",
            "title": "Already handled",
            "price": 10,
            "itemWebUrl": "https://example.com/1",
            "imageUrl": "",
            "description": "",
        }
        script = f"""
const calls = [];
const responses = [
  {{status: 200, body: {{sha: 'seen-sha', encoding: 'base64', content: {json.dumps(database_content)}}}}},
  {{status: 200, body: {{sha: 'queue-sha', content: {json.dumps(queue_content)}}}}}
];
global.fetch = async (url, options = {{}}) => {{
  calls.push(url);
  const next = responses.shift();
  return {{status: next.status, ok: true, json: async () => next.body}};
}};
process.env.GITHUB_TOKEN = 'test-token';
process.env.GITHUB_REPO = 'owner/repo';
process.env.SCOUT_INGEST_SECRET = 'test-secret';
const handler = require({json.dumps(SCOUT_MODULE)});
const req = {{method: 'POST', headers: {{'x-scout-secret': 'test-secret'}}, body: {{listings: [{json.dumps(listing)}]}}}};
let responseText = '';
const res = {{statusCode: 0, headers: {{}}, setHeader(name, value) {{this.headers[name] = value;}}, end(value) {{responseText = value.toString('utf8');}}}};
Promise.resolve(handler(req, res)).then(() => process.stdout.write(JSON.stringify({{
  status: res.statusCode, body: JSON.parse(responseText), calls
}}))).catch(error => {{process.stderr.write(error.stack);process.exit(2);}});
"""
        result = run_node_script(script)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"]["accepted"], 0)
        self.assertEqual(result["body"]["duplicates"], 1)
        self.assertEqual(result["body"]["capacityDropped"], 0)
        self.assertTrue(result["calls"][0].endswith("/contents/seen_items.db"))
        self.assertTrue(result["calls"][1].endswith("/contents/scout_queue.jsonl"))

    def test_queue_full_response_includes_retry_after_contract(self):
        script = (
            f"const t=require({json.dumps(SCOUT_MODULE)})._test;"
            "let text='';const res={statusCode:0,headers:{},setHeader(k,v){this.headers[k]=v},"
            "end(v){text=v.toString('utf8')}};"
            "t.sendQueueFull(res,{error:'full'});"
            "process.stdout.write(JSON.stringify({status:res.statusCode,headers:res.headers,body:JSON.parse(text)}))"
        )
        completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
        result = json.loads(completed.stdout)
        self.assertEqual(result["status"], 429)
        self.assertEqual(result["headers"]["Retry-After"], "600")
        self.assertEqual(result["body"]["retryAfterSeconds"], 600)

    def test_listing_urls_reject_credentials_and_non_http_schemes(self):
        base = {"platform": "facebook", "itemId": "1", "title": "A", "price": 1, "itemWebUrl": "https://example.com/1", "imageUrl": "", "description": ""}
        payload = json.dumps(base)
        expression = (
            f"(()=>{{const s=require({json.dumps(SCOUT_MODULE)})._test;const a={payload};"
            "const good=s.validateListing(a,0);"
            "const badScheme=s.validateListing({...a,itemWebUrl:'file:///etc/passwd'},0);"
            "const badAuth=s.validateListing({...a,imageUrl:'https://user:pass@example.com/x'},0);"
            "return [good,badScheme,badAuth]})()"
        )
        script = f"Promise.resolve({expression}).then(v=>process.stdout.write(JSON.stringify(v)))"
        completed = subprocess.run(["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True)
        values = json.loads(completed.stdout)
        self.assertIsNone(values[0])
        self.assertIn("HTTP(S)", values[1])
        self.assertIn("HTTP(S)", values[2])


class ConfigApiTests(unittest.TestCase):
    def test_repeated_bad_passwords_trigger_per_ip_backoff(self):
        script = f"""
process.env.GITHUB_TOKEN='test-token';process.env.GITHUB_REPO='owner/repo';process.env.SETTINGS_PASSWORD='right';delete process.env.NTFY_TOPIC;
const handler=require({json.dumps(CONFIG_MODULE)});
const invoke=async()=>{{let text='';const res={{statusCode:0,headers:{{}},setHeader(k,v){{this.headers[k]=v}},end(v){{text=v.toString('utf8')}}}};await handler({{method:'GET',headers:{{'x-settings-password':'wrong','x-forwarded-for':'203.0.113.4'}}}},res);return {{status:res.statusCode,headers:res.headers}}}};
(async()=>{{const results=[];for(let i=0;i<4;i++)results.push(await invoke());process.stdout.write(JSON.stringify(results))}})().catch(e=>{{process.stderr.write(e.stack);process.exit(2)}});
"""
        results = run_node_script(script)
        self.assertEqual([result["status"] for result in results], [401, 401, 401, 429])
        self.assertEqual(results[2]["headers"]["Retry-After"], "1")

    def test_saved_search_and_keyword_fields_are_strictly_validated(self):
        cases = []

        body = valid_config()
        body["SAVED_SEARCHES"][0]["query"] = "   "
        cases.append((body, "SAVED_SEARCHES[0].query must be a non-empty string"))

        body = valid_config()
        del body["SAVED_SEARCHES"][0]["max_price"]
        cases.append((body, "SAVED_SEARCHES[0].max_price must be a non-negative finite number"))

        body = valid_config()
        body["SAVED_SEARCHES"][0]["max_price"] = -1
        cases.append((body, "SAVED_SEARCHES[0].max_price must be a non-negative finite number"))

        body = valid_config()
        body["SAVED_SEARCHES"][0]["max_price"] = float("inf")
        cases.append((body, "SAVED_SEARCHES[0].max_price must be a non-negative finite number"))

        body = valid_config()
        body["STANDARD_BRANDS"].append(42)
        cases.append((body, "STANDARD_BRANDS[1] must be a string"))

        body = valid_config()
        body["SAVED_SEARCHES"][0]["size"] = "M"
        cases.append((body, "SAVED_SEARCHES[0].size must be a string array or null"))

        body = valid_config()
        body["SAVED_SEARCHES"][0]["size"] = ["M", 42]
        cases.append((body, "SAVED_SEARCHES[0].size[1] must be a string"))

        body = valid_config()
        body["SAVED_SEARCHES"][0]["profile"] = "turbo"
        cases.append((body, "SAVED_SEARCHES[0].profile must be fast or slow"))

        body = valid_config()
        body["SAVED_SEARCHES"][0]["platforms"] = ["ebay", "made-up-market"]
        cases.append((body, "SAVED_SEARCHES[0].platforms contains unknown platform made-up-market"))

        body = valid_config()
        body["SAVED_SEARCHES"].append({
            **body["SAVED_SEARCHES"][0],
            "id": "outerwear-mens-jacket-copy",
        })
        cases.append((body, "SAVED_SEARCHES[1].query duplicates active search outerwear-mens-jacket"))

        body = valid_config()
        body["SAVED_SEARCHES"][0].update({
            "id": "golf-club-set",
            "category": "golf-equipment",
            "query": "golf club set",
            "max_price": 301,
        })
        cases.append((body, "SAVED_SEARCHES[0].max_price exceeds golf-equipment hard gate 300"))

        for body, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                result = run_api_handler(
                    CONFIG_MODULE, {"config": body, "baseSha": "base-sha"}, []
                )
                self.assertEqual(result["status"], 400)
                self.assertEqual(result["body"]["error"], expected_error)
                self.assertEqual(result["calls"], [])

    def test_config_write_merges_unmodeled_current_keys(self):
        body = valid_config()
        body["PIT_TO_PIT_CAP_INCHES"] = 31
        current = valid_config()
        current["RUN_BUDGET_SECONDS"] = 390
        result = run_api_handler(
            CONFIG_MODULE,
            {"config": body, "baseSha": "base-sha"},
            [
                {"status": 200, "body": github_file([current], "base-sha") | {"content": base64.b64encode(json.dumps(current).encode()).decode()}},
                {"status": 200, "body": {"content": {"sha": "saved-sha"}}},
            ],
        )

        self.assertEqual(result["status"], 200)
        self.assertEqual([call["options"].get("method") for call in result["calls"]], ["GET", "PUT"])
        put = json.loads(result["calls"][1]["options"]["body"])
        saved = json.loads(base64.b64decode(put["content"]))
        self.assertEqual(put["sha"], "base-sha")
        self.assertEqual(saved["RUN_BUDGET_SECONDS"], 390)
        self.assertEqual(saved["PIT_TO_PIT_CAP_INCHES"], 31)
        self.assertEqual(result["body"]["sha"], "saved-sha")

    def test_config_write_rejects_stale_snapshot_without_put(self):
        current = valid_config()
        current_file = {
            "sha": "fresh-sha",
            "content": base64.b64encode(json.dumps(current).encode()).decode(),
        }
        result = run_api_handler(
            CONFIG_MODULE,
            {"config": valid_config(), "baseSha": "stale-sha"},
            [{"status": 200, "body": current_file}],
        )
        self.assertEqual(result["status"], 409)
        self.assertIn("changed since you loaded it", result["body"]["error"])
        self.assertEqual(len(result["calls"]), 1)

    def test_config_write_does_not_retry_a_put_conflict(self):
        current = valid_config()
        current_file = {
            "sha": "base-sha",
            "content": base64.b64encode(json.dumps(current).encode()).decode(),
        }
        result = run_api_handler(
            CONFIG_MODULE,
            {"config": valid_config(), "baseSha": "base-sha"},
            [
                {"status": 200, "body": current_file},
                {"status": 409, "body": {"message": "conflict"}},
            ],
        )
        self.assertEqual(result["status"], 409)
        self.assertIn("changed while you were saving", result["body"]["error"])
        self.assertEqual(len(result["calls"]), 2)


class EbayAccountDeletionApiTests(unittest.TestCase):
    def test_public_get_challenge_does_not_require_ebay_api_credentials(self):
        challenge = "challenge-123"
        token = "verification-token"
        endpoint = "https://example.vercel.app/api/ebay-account-deletion"
        script = f"""
process.env.EBAY_DELETION_VERIFICATION_TOKEN = {json.dumps(token)};
process.env.EBAY_DELETION_ENDPOINT_URL = {json.dumps(endpoint)};
delete process.env.EBAY_CLIENT_ID;
delete process.env.EBAY_CLIENT_SECRET;
const handler = require({json.dumps(DELETION_MODULE)});
const req = {{method:'GET',headers:{{}},query:{{challenge_code:{json.dumps(challenge)}}},url:'/?challenge_code=x'}};
let text='';
const res={{statusCode:0,setHeader(){{}},end(value){{text=value.toString('utf8')}}}};
Promise.resolve(handler(req,res)).then(()=>process.stdout.write(JSON.stringify({{status:res.statusCode,body:JSON.parse(text)}})));
"""
        result = run_node_script(script)
        expected = hashlib.sha256(f"{challenge}{token}{endpoint}".encode()).hexdigest()
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"], {"challengeResponse": expected})

    def test_signed_notification_round_trips_with_ebay_public_key(self):
        script = f"""
const crypto=require('crypto');
const {{publicKey,privateKey}}=crypto.generateKeyPairSync('ec',{{namedCurve:'prime256v1'}});
const body={{notification:{{data:{{userId:'user-123'}}}}}};
const signer=crypto.createSign('sha1');signer.update(JSON.stringify(body));signer.end();
const signature=signer.sign(privateKey,'base64');
const header=Buffer.from(JSON.stringify({{alg:'ecdsa',digest:'SHA1',kid:'real-key',signature}})).toString('base64');
const key=publicKey.export({{type:'spki',format:'pem'}}).replace(/\\r?\\n/g,'');
const calls=[];
const responses=[
  {{status:200,body:{{access_token:'app-token',expires_in:7200}}}},
  {{status:200,body:{{key,algorithm:'ECDSA',digest:'SHA1'}}}}
];
global.fetch=async(url,options={{}})=>{{calls.push(url);const next=responses.shift();return {{status:next.status,ok:true,json:async()=>next.body}}}};
process.env.EBAY_DELETION_VERIFICATION_TOKEN='verification-token';
process.env.EBAY_DELETION_ENDPOINT_URL='https://example.test/api/ebay-account-deletion';
process.env.EBAY_CLIENT_ID='client';process.env.EBAY_CLIENT_SECRET='secret';
const handler=require({json.dumps(DELETION_MODULE)});
const req={{method:'POST',headers:{{'x-ebay-signature':header,'x-forwarded-for':'203.0.113.8'}},body}};
let text='';const res={{statusCode:0,headers:{{}},setHeader(k,v){{this.headers[k]=v}},end(v){{text=v.toString('utf8')}}}};
Promise.resolve(handler(req,res)).then(()=>process.stdout.write(JSON.stringify({{status:res.statusCode,body:JSON.parse(text),calls}}))).catch(e=>{{process.stderr.write(e.stack);process.exit(2)}});
"""
        result = run_node_script(script)
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["body"], {"ok": True})
        self.assertEqual(len(result["calls"]), 2)

    def test_unknown_key_id_is_negative_cached_and_returns_precondition_failed(self):
        script = f"""
const calls=[];
const responses=[
  {{status:200,body:{{access_token:'app-token',expires_in:7200}}}},
  {{status:404,body:{{message:'not found'}}}}
];
global.fetch=async(url,options={{}})=>{{calls.push(url);const next=responses.shift();return {{status:next.status,ok:next.status<400,json:async()=>next.body}}}};
process.env.EBAY_DELETION_VERIFICATION_TOKEN='verification-token';
process.env.EBAY_DELETION_ENDPOINT_URL='https://example.test/api/ebay-account-deletion';
process.env.EBAY_CLIENT_ID='client';process.env.EBAY_CLIENT_SECRET='secret';
const handler=require({json.dumps(DELETION_MODULE)});
const header=Buffer.from(JSON.stringify({{alg:'ecdsa',digest:'SHA1',kid:'unknown-key',signature:'AA=='}})).toString('base64');
const invoke=async()=>{{let text='';const req={{method:'POST',headers:{{'x-ebay-signature':header,'x-forwarded-for':'203.0.113.9'}},body:{{notification:{{data:{{userId:'u'}}}}}}}};const res={{statusCode:0,setHeader(){{}},end(v){{text=v.toString('utf8')}}}};await handler(req,res);return res.statusCode}};
(async()=>{{const statuses=[await invoke(),await invoke()];process.stdout.write(JSON.stringify({{statuses,calls}}))}})().catch(e=>{{process.stderr.write(e.stack);process.exit(2)}});
"""
        result = run_node_script(script)
        self.assertEqual(result["statuses"], [412, 412])
        self.assertEqual(len(result["calls"]), 2)


class LedgerApiTests(unittest.TestCase):
    def test_prices_must_be_null_or_non_negative_finite_numbers(self):
        for field in ("bought_price", "sold_price"):
            for value in (-1, float("inf"), ""):
                with self.subTest(field=field, value=value):
                    result = run_api_handler(
                        LEDGER_MODULE, {"item_id": "123", field: value}, []
                    )
                    self.assertEqual(result["status"], 400)
                    self.assertEqual(
                        result["body"]["error"],
                        f"{field} must be a non-negative number or null",
                    )

    def test_ledger_conflict_refetches_and_reapplies_entry_merge(self):
        first_ledger = [{"item_id": "1", "title": "Original", "bought_price": 25}]
        concurrent_ledger = [
            {"item_id": "1", "title": "Concurrent", "bought_price": 25},
            {"item_id": "2", "title": "Other", "bought_price": 10},
        ]
        result = run_api_handler(
            LEDGER_MODULE,
            {"item_id": "1", "sold_price": 40, "sold_platform": "eBay"},
            [
                {"status": 200, "body": github_file(first_ledger, "old-sha")},
                {"status": 409, "body": {"message": "conflict"}},
                {"status": 200, "body": github_file(concurrent_ledger, "fresh-sha")},
                {"status": 200, "body": {"content": {"sha": "saved"}}},
            ],
        )

        self.assertEqual(result["status"], 200)
        self.assertEqual([call["options"].get("method") for call in result["calls"]], ["GET", "PUT", "GET", "PUT"])
        second_put = json.loads(result["calls"][3]["options"]["body"])
        self.assertEqual(second_put["sha"], "fresh-sha")
        saved_text = base64.b64decode(second_put["content"]).decode()
        saved_ledger = [json.loads(line) for line in saved_text.splitlines()]
        self.assertEqual(
            saved_ledger,
            [
                {
                    "item_id": "1",
                    "title": "Concurrent",
                    "bought_price": 25,
                    "sold_price": 40,
                    "sold_platform": "eBay",
                },
                {"item_id": "2", "title": "Other", "bought_price": 10},
            ],
        )
        self.assertEqual(result["body"], saved_ledger)


class MobileSettingsUiTests(unittest.TestCase):
    def test_history_payload_is_unwrapped_before_activity_consumers_run(self):
        load_data = index_javascript(
            "    async function loadData", "    function ledgerMap"
        )
        result = run_node_script(
            "let historyItems=null;let ledgerItems=null;let historySkipped=0;"
            "const responses=["
            "{ok:true,json:async()=>({history:[{item_id:'1'}],skipped:2})},"
            "{ok:true,json:async()=>[]}];"
            "const apiFetch=async()=>responses.shift();"
            + load_data
            + "loadData().then(()=>process.stdout.write(JSON.stringify({historyItems,historySkipped})));"
        )
        self.assertEqual(result["historyItems"], [{"item_id": "1"}])
        self.assertEqual(result["historySkipped"], 2)

    def test_number_value_preserves_missing_values_and_local_date_is_used(self):
        number_function = index_javascript(
            "    function numberValue", "    function money"
        )
        values = run_node_script(
            number_function
            + "process.stdout.write(JSON.stringify([numberValue(null),numberValue(undefined),numberValue(''),numberValue('   '),numberValue('0'),numberValue(0)]));"
        )
        self.assertEqual(values, [None, None, None, None, 0, 0])

        date_function = index_javascript(
            "    function todayIsoDate", "    async function saveLedgerEntry"
        )
        value = run_node_script(
            "global.Date=class {getFullYear(){return 2026}getMonth(){return 7}getDate(){return 9}toISOString(){return '2099-01-01T00:00:00Z'}};"
            + date_function
            + "process.stdout.write(JSON.stringify(todayIsoDate()));"
        )
        self.assertEqual(value, "2026-08-09")

    def test_stats_count_only_review_alerts_and_only_sold_cost_basis(self):
        helpers = index_javascript("    function escapeHtml", "    function updateListCounts")
        render_stats = index_javascript(
            "    function renderStatsSection", "    function renderActivityFilters"
        )
        html = run_node_script(
            "const ratings=['Steal','Great Deal','Good Deal','Fair','Marginal'];"
            + helpers
            + render_stats
            + "const recent=new Date(Date.now()-1000).toISOString();"
            + "const items=["
            + "{timestamp:recent,verdict:'REVIEW',deal_rating:'Steal',discount_pct:50,price:25,estimated_resale_value:100},"
            + "{timestamp:recent,verdict:'PASS',deal_rating:'Great Deal',discount_pct:99,price:1,estimated_resale_value:1000}];"
            + "const ledger=[{bought_price:25,sold_price:40},{bought_price:100,sold_price:null}];"
            + "process.stdout.write(JSON.stringify(renderStatsSection(items,ledger,true)));"
        )
        self.assertEqual(html.count('<div class="stat-value">1</div>'), 3)
        self.assertIn("Avg discount: 50%", html)
        self.assertNotIn("Avg discount: 75%", html)
        self.assertIn("last 30 days were bought", html)
        self.assertIn('<div class="stat-value">$75</div>', html)
        self.assertIn('<div class="stat-value">$15</div>', html)
        self.assertIn("Gross sales $40 | sold cost basis $25", html)
        self.assertIn("Cost $100 | bought but not yet sold", html)

    def test_blank_and_negative_prompt_prices_are_rejected_before_save(self):
        mark_bought = index_javascript(
            "    async function markBought", "    async function markSold"
        )
        mark_sold = index_javascript(
            "    async function markSold", "    function renderActivity"
        )
        result = run_node_script(
            "let promptValue='';let statuses=[];let saves=0;"
            + "const window={prompt:()=>promptValue};"
            + "const ledgerMap=()=>new Map();const findHistoryItem=()=>({});"
            + "const showStatus=(message,kind)=>statuses.push([message,kind]);"
            + "const clearStatus=()=>{};const saveLedgerEntry=async()=>{saves+=1};"
            + mark_bought
            + mark_sold
            + "(async()=>{await markBought('1');promptValue='-1';await markSold('1');"
            + "return {statuses,saves}})().then(value=>process.stdout.write(JSON.stringify(value)));"
        )
        self.assertEqual(result["saves"], 0)
        self.assertEqual(
            result["statuses"],
            [
                ["Bought price must be a non-negative number.", "error"],
                ["Sold price must be a non-negative number.", "error"],
            ],
        )


if __name__ == "__main__":
    unittest.main()
