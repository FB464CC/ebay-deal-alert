"""Small Node-backed regression tests for security-critical web API helpers."""

import base64
import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TELEGRAM_MODULE = (ROOT / "web" / "api" / "telegram.js").as_posix()
URL_UTILS_MODULE = (ROOT / "chrome-extension" / "url-utils.js").as_posix()
SCOUT_MODULE = (ROOT / "web" / "api" / "scout-ingest.js").as_posix()
CONFIG_MODULE = (ROOT / "web" / "api" / "config.js").as_posix()
LEDGER_MODULE = (ROOT / "web" / "api" / "ledger.js").as_posix()
INDEX_HTML = ROOT / "web" / "index.html"


def run_node(expression):
    script = (
        f"const t=require({json.dumps(TELEGRAM_MODULE)})._test;"
        f"Promise.resolve({expression}).then(v=>process.stdout.write(JSON.stringify(v)))"
        ".catch(e=>{process.stderr.write(e.message);process.exit(2)})"
    )
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True
    )
    return json.loads(completed.stdout)


def run_node_script(script):
    completed = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True, text=True, check=True
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
        "SAVED_SEARCHES": [
            {"query": "men's jacket", "size": ["M", "L"], "max_price": 50}
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

        for body, expected_error in cases:
            with self.subTest(expected_error=expected_error):
                result = run_api_handler(CONFIG_MODULE, body, [])
                self.assertEqual(result["status"], 400)
                self.assertEqual(result["body"]["error"], expected_error)
                self.assertEqual(result["calls"], [])

    def test_config_write_retries_conflict_with_fresh_sha(self):
        body = valid_config()
        result = run_api_handler(
            CONFIG_MODULE,
            body,
            [
                {"status": 200, "body": {"sha": "old-sha"}},
                {"status": 409, "body": {"message": "conflict"}},
                {"status": 200, "body": {"sha": "fresh-sha"}},
                {"status": 200, "body": {"content": {"sha": "saved"}}},
            ],
        )

        self.assertEqual(result["status"], 200)
        self.assertEqual([call["options"].get("method") for call in result["calls"]], ["GET", "PUT", "GET", "PUT"])
        first_put = json.loads(result["calls"][1]["options"]["body"])
        second_put = json.loads(result["calls"][3]["options"]["body"])
        self.assertEqual(first_put["sha"], "old-sha")
        self.assertEqual(second_put["sha"], "fresh-sha")
        self.assertEqual(json.loads(base64.b64decode(second_put["content"])), body)

    def test_config_write_stops_after_two_conflict_retries(self):
        responses = []
        for attempt in range(3):
            responses.extend(
                [
                    {"status": 200, "body": {"sha": f"sha-{attempt}"}},
                    {"status": 409, "body": {"message": "still conflicted"}},
                ]
            )
        result = run_api_handler(CONFIG_MODULE, valid_config(), responses)
        self.assertEqual(result["status"], 409)
        self.assertEqual(result["body"]["error"], "still conflicted")
        self.assertEqual(len(result["calls"]), 6)


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
