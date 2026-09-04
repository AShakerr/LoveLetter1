import { test } from "node:test";
import assert from "node:assert/strict";
import { normalizeEgyptPhone, displayPhone } from "../phone";

test("normalizes common Egyptian formats", () => {
  assert.equal(normalizeEgyptPhone("01012345678"), "+201012345678");
  assert.equal(normalizeEgyptPhone("+20 101 234 5678"), "+201012345678");
  assert.equal(normalizeEgyptPhone("00201012345678"), "+201012345678");
  assert.equal(normalizeEgyptPhone("201512345678"), "+201512345678");
  assert.equal(normalizeEgyptPhone("٠١٠١٢٣٤٥٦٧٨"), "+201012345678");
});

test("rejects non-mobile or foreign numbers", () => {
  assert.equal(normalizeEgyptPhone("0212345678"), null); // Cairo landline
  assert.equal(normalizeEgyptPhone("+14155551234"), null);
  assert.equal(normalizeEgyptPhone("0101234567"), null); // too short
  assert.equal(normalizeEgyptPhone("01312345678"), null); // 013 not a mobile prefix
});

test("displayPhone formats local style", () => {
  assert.equal(displayPhone("+201012345678"), "0101 234 5678");
});
