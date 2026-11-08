// Run with: node --test tests/test_dashboard_dom.mjs
// A DOM double that rejects HTML parsing makes this rendering contract explicit.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.classList = { toggle() {} };
  }
  append(...children) {
    for (const child of children) {
      if (child instanceof Element && child.tag === "fragment") this.append(...child.children);
      else this.children.push(child instanceof Element ? child : String(child));
    }
  }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  insertBefore(child, before) { this.children.splice(this.children.indexOf(before), 0, child); }
  set textContent(value) { this.replaceChildren(value); }
  get childNodes() { return this.children; }
  get textContent() { return this.children.map((child) => child instanceof Element ? child.textContent : child).join(""); }
  set innerHTML(value) { throw new Error(`Unsafe HTML sink: ${value}`); }
  setAttribute(key, value) { this[key] = String(value); }
  addEventListener() {}
}

function dashboard() {
  const elements = new Map();
  const context = vm.createContext({
    window: { location: { pathname: "/dashboard/local" }, setInterval() {} },
    sessionStorage: { getItem() { return ""; } },
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, new Element("div"));
        return elements.get(selector);
      },
      createElement: (tag) => new Element(tag),
      createElementNS: (_namespace, tag) => new Element(tag),
      createDocumentFragment: () => new Element("fragment"),
      addEventListener() {},
    },
  });
  vm.runInContext(readFileSync(new URL("../src/bespokelabs/sandbox/control_plane/static/dashboard.js", import.meta.url), "utf8"), context);
  return { context, elements };
}

test("sandbox detail treats API strings as text and retains controls and cost labels", () => {
  const { context, elements } = dashboard();
  const attack = '<img src=x onerror="globalThis.attacked=true"><script>alert(1)</script>';
  context.detail = {
    sandbox: { id: attack, status: "running", backend: attack, provider_resource_id: attack, creator_api_key_name: attack, version: attack, latest_error: { code: attack, message: attack } },
    cost: { cost_state: attack },
    agent_usage: { runs: 1, unknown_usage_runs: 0, estimated_cost_runs: 1, estimated_cost_usd: "2.135", total_tokens: 1050000 },
    agent_runs: [{ harness: attack, model: attack, tokens_known: true, total_tokens: 1050000, cost_state: "estimated", cost_usd: "2.135", cost_reason: attack, pricing_as_of: attack, rates_per_million: { input: attack, cached_input: ".175", output: "14" } }],
    attempts: [{ attempt_number: attack, status: attack, error: { message: attack } }],
    executions: [{ request_id: attack, status: attack, response: { exit_code: attack, stdout: attack } }],
    provider_observations: [{ status: attack }],
  };
  vm.runInContext("state.session = { can_terminate: true }; renderDetail(detail)", context);
  const panel = elements.get("#detailContent");
  assert.ok(panel.textContent.includes(attack));
  assert.ok(panel.textContent.includes("Codex estimate · $2.14"));
  assert.ok(panel.textContent.includes("1,050,000 tokens"));
  const all = (element) => [element, ...element.children.filter((child) => child instanceof Element).flatMap(all)];
  const nodes = all(panel);
  assert.equal(nodes.some((element) => ["img", "script"].includes(element.tag)), false);
  const link = nodes.find((element) => element.tag === "a");
  assert.equal(link.href, "https://developers.openai.com/api/docs/pricing");
  const button = elements.get("#detailFooter").children[1];
  assert.equal(button.tag, "button");
  assert.equal(button.dataset.id, attack);
  assert.equal(button.dataset.version, attack);
  vm.runInContext("state.session = {}; renderDetail(detail)", context);
  assert.equal(elements.get("#detailFooter").children[1].textContent, "Read-only access");
});

test("model overview renders both usage and permission states without HTML sinks", () => {
  const { context, elements } = dashboard();
  vm.runInContext("renderAgentOverview([])", context);
  assert.match(elements.get("#agentUsageSummary").textContent, /requires usage access/);
  vm.runInContext("state.session = { can_view_usage: true }; renderAgentOverview([{ agent_usage: { runs: 1, unknown_usage_runs: 1, unknown_cost_runs: 1 } }])", context);
  assert.match(elements.get("#agentUsageSummary").textContent, /unavailable cost/);
  assert.match(elements.get("#agentUsageSummary").textContent, /Unavailable/);
});

test("resource charts preserve zero, gaps, stale labels and safe GPU names", () => {
  const { context } = dashboard();
  const attack = '<img src=x onerror="alert(1)">';
  context.data = { status: "stale", interval_seconds: 15, samples: [
    { collected_at: "2026-11-08T00:00:00Z", cpu_percent: 50, memory_used_bytes: 1073741824, gpus: [{ id: "gpu", name: attack, utilization_percent: 10, memory_used_bytes: 1000 }] },
    { collected_at: "2026-11-08T00:00:15Z", cpu_percent: null, memory_used_bytes: null, gpus: [] },
    { collected_at: "2026-11-08T00:00:30Z", cpu_percent: 0, memory_used_bytes: 0, gpus: [{ id: "gpu", name: attack, utilization_percent: null, memory_used_bytes: 2000 }] },
  ] };
  const content = vm.runInContext("resourceContent(data)", context);
  assert.match(content.textContent, /Stale/);
  assert.match(content.textContent, /0\.0 %/);
  assert.match(content.textContent, /GPU utilizationUnavailable/);
  assert.ok(content.textContent.includes(attack));
  const all = (element) => [element, ...element.children.filter((child) => child instanceof Element).flatMap(all)];
  const nodes = all(content);
  assert.equal(nodes.some((element) => element.tag === "img"), false);
  const cpu = nodes.find((element) => element.tag === "svg");
  assert.equal(all(cpu).filter((element) => element.tag === "circle").length, 2);
  assert.equal(all(cpu).filter((element) => element.tag === "polyline").length, 0);
  context.data.samples = [];
  assert.match(vm.runInContext("resourceContent(data)", context).textContent, /No measurements/);
  context.data.status = "disabled";
  context.data.interval_seconds = 0;
  const disabled = vm.runInContext("resourceContent(data)", context).textContent;
  assert.match(disabled, /Sampling is disabled/);
  assert.doesNotMatch(disabled, /Sampled every/);
});

test("short resource windows align observations with time labels", () => {
  const { context } = dashboard();
  const all = (element) => [element, ...element.children.filter((child) => child instanceof Element).flatMap(all)];
  context.samples = [
    { collected_at: "2026-11-08T00:00:00Z", cpu_percent: 50 },
    { collected_at: "2026-11-08T00:00:05Z", cpu_percent: 75 },
  ];
  const chart = vm.runInContext('resourceChart("CPU", samples, (sample) => sample.cpu_percent, "%", 100)', context);
  const line = all(chart).find((element) => element.tag === "polyline");
  assert.equal(line.points.split(" ")[0].split(",")[0], "42");
  assert.equal(line.points.split(" ").at(-1).split(",")[0], "352");
  context.samples = [context.samples[0]];
  const single = vm.runInContext('resourceChart("CPU", samples, (sample) => sample.cpu_percent, "%", 100)', context);
  assert.equal(all(single).find((element) => element.tag === "circle").cx, "352");
  assert.equal(all(single).filter((element) => element.tag === "text" && element.y === "148").length, 1);
});
