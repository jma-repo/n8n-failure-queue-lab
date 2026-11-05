const test = require("node:test");
const assert = require("node:assert/strict");
const { classifyFetch, parseBody, parseRetryAfter } = require("../../n8n/code/validate.js");

test("a 2xx answer is not a fetch failure", () => {
  for (const statusCode of [200, 204, 299]) assert.equal(classifyFetch({ statusCode }), null);
});

test("5xx statuses are http_5xx and keep the status", () => {
  for (const statusCode of [500, 502, 503, 504, 599]) {
    const f = classifyFetch({ statusCode, statusMessage: "oops" });
    assert.equal(f.error_type, "http_5xx");
    assert.equal(f.http_status, statusCode);
  }
});

test("429 is rate_limited and honours Retry-After in seconds or as a date, capped at an hour", () => {
  assert.equal(classifyFetch({ statusCode: 429, headers: { "retry-after": "30" } }).retry_after_s, 30);
  assert.equal(classifyFetch({ statusCode: 429, headers: { "Retry-After": "999999" } }).retry_after_s, 3600);
  const soon = new Date(Date.now() + 120000).toUTCString();
  const s = classifyFetch({ statusCode: 429, headers: { "retry-after": soon } }).retry_after_s;
  assert.ok(s > 100 && s <= 120, `got ${s}`);
  assert.equal(classifyFetch({ statusCode: 429, headers: {} }).retry_after_s, null);
  assert.equal(parseRetryAfter("not a date"), null);
});

test("4xx statuses other than 429 and 408 are http_4xx, which retrying cannot fix", () => {
  for (const statusCode of [400, 401, 403, 404, 410]) assert.equal(classifyFetch({ statusCode }).error_type, "http_4xx");
  assert.equal(classifyFetch({ statusCode: 408 }).error_type, "timeout");
  assert.equal(classifyFetch({ statusCode: 302 }).error_type, "http_4xx");
});

test("an error item from the HTTP node is a timeout or a network failure by its message", () => {
  assert.equal(classifyFetch({ error: "timeout of 10000ms exceeded" }).error_type, "timeout");
  assert.equal(classifyFetch({ error: { message: "ECONNABORTED" } }).error_type, "timeout");
  assert.equal(classifyFetch({ error: "The service refused the connection - perhaps it is offline" }).error_type, "network");
  assert.equal(classifyFetch({ error: { code: "ENOTFOUND", note: "getaddrinfo" } }).error_type, "network");
  assert.equal(classifyFetch(undefined).error_type, "network");
  assert.equal(classifyFetch(null).error_type, "network");
});

test("long details are cut so a queue row stays small", () => {
  assert.ok(classifyFetch({ error: "x".repeat(5000) }).detail.length <= 300);
});

test("parseBody copes with a BOM, padding, objects and rubbish", () => {
  assert.deepEqual(parseBody('﻿  {"a": 1}\n').value, { a: 1 });
  assert.deepEqual(parseBody({ a: 1 }).value, { a: 1 });
  for (const rubbish of ["", "   ", null, undefined, "<html></html>", '{"a": ', "not json"]) {
    const r = parseBody(rubbish);
    assert.equal(r.ok, false, JSON.stringify(rubbish));
    assert.ok(r.detail.length > 0);
  }
  assert.match(parseBody("<html>down</html>").detail, /starts with: <html>/);
});
