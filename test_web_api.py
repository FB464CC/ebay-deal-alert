"""Small Node-backed regression tests for security-critical web API helpers."""

import json
import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TELEGRAM_MODULE = (ROOT / "web" / "api" / "telegram.js").as_posix()
URL_UTILS_MODULE = (ROOT / "chrome-extension" / "url-utils.js").as_posix()


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


if __name__ == "__main__":
    unittest.main()
