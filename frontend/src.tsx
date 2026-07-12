import React, { useEffect, useMemo, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  BarChart3,
  Box,
  CheckCircle2,
  ClipboardList,
  Database,
  Download,
  FileCode2,
  Gauge,
  Play,
  RefreshCw,
  Save,
  Settings as SettingsIcon,
  ShieldCheck,
  Square,
  Trash2,
  Upload,
  Wifi,
} from "lucide-react";
import "./style.css";

type TaskChoice = {
  id: string;
  task_number?: number | null;
  suite_id: string;
  external_id?: string;
  title: string;
  language?: string;
  category?: string;
  available: boolean;
  official_pass_rate?: number | null;
  official_avg_duration_seconds?: number | null;
};
type Boot = {
  defaults: {
    agent: string;
    model: string;
    reasoning_effort: string;
    concurrency: number;
  };
  agents: string[];
  models: string[];
  efforts: string[];
  service_tiers: string[];
  setting_options: {
    credential_files: string[];
    jobs_dirs: string[];
    concurrency: number[];
  };
  task_suite: { name: string; tasks: TaskChoice[] };
};
type Check = { name: string; status: string; message: string };
type Trial = {
  id: string;
  task: string;
  task_slug?: string;
  task_number?: number | null;
  task_code?: string;
  task_title?: string;
  attempt?: number;
  run_code?: string;
  trial_code?: string;
  resource_name?: string | null;
  status: string;
  reward: number | null;
  partial: number | null;
  f2p: number | null;
  p2p: number | null;
  duration_seconds: number | null;
  agent_duration_seconds: number | null;
  input_tokens: number | null;
  cached_tokens: number | null;
  output_tokens: number | null;
  reported_cost_usd: number | null;
  steps: number | null;
  patch: string;
  patch_bytes: number;
  failure_type?: string;
  failure_message?: string;
};
type Run = {
  id: number;
  run_code?: string;
  status: string;
  stage?: string;
  job_name: string;
  agent: string;
  model: string;
  reasoning_effort: string;
  reward: number | null;
  reported_cost_usd: number | null;
  estimated_cost_usd: number | null;
  created_at: string;
  tasks: string[];
  attempts_per_task: number;
  concurrency: number;
  infrastructure_max_retries?: number;
  claude_max_turns?: number;
  codex_request_max_retries?: number;
  codex_stream_max_retries?: number;
  codex_stream_idle_timeout_seconds?: number;
  progress?: {
    completed: number;
    total: number;
    passed: number;
    percent: number;
  };
  trials?: Trial[];
  is_baseline?: boolean;
  baseline_name?: string;
  regression?: {
    level: string;
    reasons: string[];
    baseline_name: string;
    pass_rate_delta: number;
    current_pass_rate?: number | null;
    baseline_pass_rate?: number | null;
    current_duration_seconds?: number | null;
    baseline_duration_seconds?: number | null;
    baseline_trials?: number | null;
    baseline_type?: "official" | "custom";
  } | null;
  input_tokens?: number;
  cached_tokens?: number;
  uncached_input_tokens?: number;
  output_tokens?: number;
  error?: string;
};
type TaskInfo = {
  id: string;
  task_number: number;
  code: string;
  title: string;
  description: string;
  language?: string;
  category?: string;
  local_trials: number;
  local_pass_rate: number | null;
  last_failure?: string;
  official_pass_rate?: number | null;
  official_avg_duration_seconds?: number | null;
  official_trials?: number | null;
};
type Prefs = {
  credential_file: string;
  jobs_dir: string;
  default_agent: string;
  default_model: string;
  default_effort: string;
  default_concurrency: number;
  docker_cleanup_after_run: boolean;
  docker_cleanup_on_delete: boolean;
  docker_cache_retention_hours: number;
  docker_cache_warning_gb: number;
  run_budget_usd: number;
};
type DockerStorage = {
  available: boolean;
  error?: string;
  images?: {
    count: number;
    size_bytes: number | null;
    reclaimable_bytes: number | null;
    managed_count: number;
    orphaned_count: number;
  };
  build_cache?: {
    count: number;
    size_bytes: number | null;
    reclaimable_bytes: number | null;
  };
  active_runs: number;
};
// 由后端 8765 直接服务（或反向代理无端口）时用相对路径；vite dev/preview 的任意端口都指向后端
const apiBase =
  location.port && location.port !== "8765" ? "http://127.0.0.1:8765" : "";
const request = async <T,>(path: string, options?: RequestInit): Promise<T> => {
  const response = await fetch(apiBase + path, options);
  if (!response.ok) {
    const text = await response.text();
    let message = text;
    try {
      const data = JSON.parse(text);
      message =
        typeof data.detail === "string"
          ? data.detail
          : JSON.stringify(data.detail ?? data);
    } catch {
      // 非 JSON 错误体（如代理 502 页面）原样展示
    }
    throw new Error(`HTTP ${response.status}: ${message}`);
  }
  return response.json();
};
const money = (value: number | null | undefined) =>
  value == null ? "—" : `$${value.toFixed(4)}`;
const number = (value: number | null | undefined) =>
  value == null ? "—" : value.toLocaleString();
const percent = (value: number | null | undefined) =>
  value == null ? "—" : `${(value * 100).toFixed(2)}%`;
const duration = (seconds: number | null | undefined) =>
  seconds == null
    ? "—"
    : seconds < 60
      ? `${Math.round(seconds)}s`
      : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);

function Status({ value }: { value: string }) {
  return (
    <span className={`status ${value}`}>{value.replaceAll("_", " ")}</span>
  );
}
function Metric({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="metric">
      <span>{label}</span>
      <strong>{value}</strong>
      {hint && <small>{hint}</small>}
    </div>
  );
}

function App() {
  const [boot, setBoot] = useState<Boot>();
  const [checks, setChecks] = useState<Check[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [tab, setTab] = useState("dashboard");
  const [selectedRun, setSelectedRun] = useState<number | null>(null);
  const [detail, setDetail] = useState<Run | null>(null);
  const [trialLog, setTrialLog] = useState("");
  const [activeTrial, setActiveTrial] = useState<Trial | null>(null);
  const [selectedResults, setSelectedResults] = useState<number[]>([]);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [comparison, setComparison] = useState<any>();
  const [tasks, setTasks] = useState<TaskInfo[]>([]);
  const [prefs, setPrefs] = useState<Prefs>();
  const [notice, setNotice] = useState("");
  const [starting, setStarting] = useState(false);
  const [agent, setAgent] = useState("mini-swe-agent");
  const [allAgents, setAllAgents] = useState(false);
  const [model, setModel] = useState("gpt-5.6-sol");
  const [effort, setEffort] = useState("high");
  const [concurrency, setConcurrency] = useState(2);
  // 自由数字输入以字符串保存，允许中途清空；提交时统一解析校验
  const [attempts, setAttempts] = useState("1");
  const [selectedTasks, setSelectedTasks] = useState<string[]>([]);
  const [agentTimeout, setAgentTimeout] = useState("5400");
  const [verifierTimeout, setVerifierTimeout] = useState("1800");
  const [infrastructureMaxRetries, setInfrastructureMaxRetries] = useState("2");
  const [claudeMaxTurns, setClaudeMaxTurns] = useState("120");
  const [codexRequestMaxRetries, setCodexRequestMaxRetries] = useState("6");
  const [codexStreamMaxRetries, setCodexStreamMaxRetries] = useState("6");
  const [codexStreamIdleTimeout, setCodexStreamIdleTimeout] = useState("600");
  const [retry, setRetry] = useState(true);
  const [verification, setVerification] = useState(true);
  const [tier, setTier] = useState("standard");
  const refreshRuns = () =>
    request<Run[]>("/api/runs")
      .then(setRuns)
      .catch(() => {});
  const refresh = () => {
    refreshRuns();
    request<{ checks: Check[] }>("/api/diagnostics")
      .then((x) => setChecks(x.checks))
      .catch(() => {});
  };
  useEffect(() => {
    request<Boot>("/api/bootstrap").then((x) => {
      setBoot(x);
      setAgent(x.defaults.agent);
      setModel(x.defaults.model);
      setEffort(x.defaults.reasoning_effort);
      setConcurrency(x.defaults.concurrency);
      setSelectedTasks([]);
    });
    refresh();
    const id = setInterval(refreshRuns, 4000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => {
    if (!selectedRun) return;
    let closed = false;
    let source: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let latestStatus = "";
    const load = () =>
      request<Run>(`/api/runs/${selectedRun}`)
        .then((d) => {
          if (closed) return; // 快速切换运行时丢弃过期响应
          latestStatus = d.status;
        setDetail(terminal.has(d.status) && tab === "live" ? null : d);
        })
        .catch(() => {});
    const connect = () => {
      if (closed) return;
      source = new EventSource(apiBase + `/api/runs/${selectedRun}/events`);
      source.onmessage = (e) => {
        const next = JSON.parse(e.data);
        latestStatus = next.status;
        // SSE 后端已附带 regression；万一缺失则保留上次值，避免回归横幅闪现后被抹掉
        setDetail((prev) => terminal.has(next.status) && tab === "live" ? null : ({
          ...next,
          regression: next.regression ?? prev?.regression ?? null,
        }));
        refreshRuns();
        if (terminal.has(next.status)) source?.close();
      };
      source.onerror = () => {
        if (closed || terminal.has(latestStatus)) {
          source?.close();
          return;
        }
        // CONNECTING 状态浏览器会自动重连；服务器拒绝（CLOSED）时延迟重建并补拉一次详情
        if (source && source.readyState === EventSource.CLOSED) {
          retryTimer = setTimeout(() => {
            load();
            connect();
          }, 5000);
        }
      };
    };
    load();
    connect();
    return () => {
      closed = true;
      clearTimeout(retryTimer);
      source?.close();
    };
  }, [selectedRun, tab]);
  useEffect(() => {
    if (tab === "tasks" && !tasks.length)
      request<TaskInfo[]>("/api/tasks").then(setTasks);
    if (tab === "settings" && !prefs)
      request<Prefs>("/api/settings").then(setPrefs);
  }, [tab]);
  useEffect(() => {
    if (compareIds.length)
      request(
        `/api/compare?${compareIds.map((id) => `items=${encodeURIComponent(id)}`).join("&")}`,
      ).then(setComparison);
    else setComparison(undefined);
  }, [compareIds]);
  const latest = runs[0];
  const attemptsNum = parseInt(attempts, 10);
  const start = async () => {
    if (starting) return;
    if (!selectedTasks.length) return setNotice("至少选择一个任务");
    const agentTimeoutNum = parseInt(agentTimeout, 10);
    const verifierTimeoutNum = parseInt(verifierTimeout, 10);
    const infrastructureMaxRetriesNum = parseInt(infrastructureMaxRetries, 10);
    const claudeMaxTurnsNum = parseInt(claudeMaxTurns, 10);
    const codexRequestMaxRetriesNum = parseInt(codexRequestMaxRetries, 10);
    const codexStreamMaxRetriesNum = parseInt(codexStreamMaxRetries, 10);
    const codexStreamIdleTimeoutNum = parseInt(codexStreamIdleTimeout, 10);
    if (!Number.isFinite(attemptsNum) || attemptsNum < 1 || attemptsNum > 10)
      return setNotice("每题次数需为 1-10 的整数");
    if (
      !Number.isFinite(agentTimeoutNum) ||
      agentTimeoutNum < 60 ||
      agentTimeoutNum > 21600
    )
      return setNotice("Agent 超时需为 60-21600 秒");
    if (
      !Number.isFinite(verifierTimeoutNum) ||
      verifierTimeoutNum < 60 ||
      verifierTimeoutNum > 7200
    )
      return setNotice("Verifier 超时需为 60-7200 秒");
    if (!Number.isFinite(infrastructureMaxRetriesNum) || infrastructureMaxRetriesNum < 0 || infrastructureMaxRetriesNum > 6)
      return setNotice("基础设施重试次数需为 0-6 的整数");
    if (!Number.isFinite(claudeMaxTurnsNum) || claudeMaxTurnsNum < 20 || claudeMaxTurnsNum > 200)
      return setNotice("Claude 最大轮数需为 20-200 的整数");
    if (!Number.isFinite(codexRequestMaxRetriesNum) || codexRequestMaxRetriesNum < 0 || codexRequestMaxRetriesNum > 10)
      return setNotice("Codex HTTP 重试次数需为 0-10 的整数");
    if (!Number.isFinite(codexStreamMaxRetriesNum) || codexStreamMaxRetriesNum < 0 || codexStreamMaxRetriesNum > 10)
      return setNotice("Codex 流重试次数需为 0-10 的整数");
    if (!Number.isFinite(codexStreamIdleTimeoutNum) || codexStreamIdleTimeoutNum < 30 || codexStreamIdleTimeoutNum > 1800)
      return setNotice("Codex 流空闲超时需为 30-1800 秒");
    if (
      concurrency >= 4 &&
      !confirm(
        "并发 4 属于高资源占用（每个 Trial 声明 2 CPU / 8 GB 内存），可能导致本机过载。\n确认继续？",
      )
    )
      return;
    setStarting(true);
    setNotice("正在创建运行…");
    const agents = allAgents ? boot!.agents : [agent];
    const results = await Promise.allSettled(
      agents.map((current) =>
        request<Run>("/api/runs", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            agent: current,
            model,
            reasoning_effort: effort,
            tasks: selectedTasks,
            attempts_per_task: attemptsNum,
            concurrency,
            confirm_high_concurrency: concurrency >= 4,
            agent_timeout_seconds: agentTimeoutNum,
            verifier_timeout_seconds: verifierTimeoutNum,
            retry_infrastructure_errors: retry,
            infrastructure_max_retries: infrastructureMaxRetriesNum,
            claude_max_turns: claudeMaxTurnsNum,
            codex_request_max_retries: codexRequestMaxRetriesNum,
            codex_stream_max_retries: codexStreamMaxRetriesNum,
            codex_stream_idle_timeout_seconds: codexStreamIdleTimeoutNum,
            verification,
            service_tier: tier,
          }),
        }),
      ),
    );
    setStarting(false);
    const created = results
      .filter(
        (r): r is PromiseFulfilledResult<Run> => r.status === "fulfilled",
      )
      .map((r) => r.value);
    const failed = results.filter((r) => r.status === "rejected");
    if (created.length) {
      setSelectedRun(created[0].id);
      setActiveTrial(null);
      setTrialLog("");
      setTab("live");
      refreshRuns();
    }
    if (failed.length) {
      setNotice(
        `已创建 ${created.length} 个运行，${failed.length} 个失败：${String(
          (failed[0] as PromiseRejectedResult).reason,
        )}`,
      );
    } else {
      setNotice(
        allAgents
          ? `已并行创建 ${created.length} 个 Agent 运行（总 worker 上限 ${created.length * concurrency}）`
          : "运行已创建",
      );
    }
  };
  const openRun = (id: number) => {
    setSelectedRun(id);
    setActiveTrial(null);
    setTrialLog("");
    setTab("results");
  };
  const cancelCurrent = async () => {
    if (!detail) return;
    const cancelledId = detail.id;
    let outcome: { cancelled: boolean };
    try {
      outcome = await request<{ cancelled: boolean }>(
        `/api/runs/${cancelledId}/cancel`,
        { method: "POST" },
      );
    } catch (e) {
      setNotice(`取消请求失败：${String(e)}`);
      return;
    }
    const nextRuns = await request<Run[]>("/api/runs");
    setRuns(nextRuns);
    const next = nextRuns.find((run) => !terminal.has(run.status));
    if (next) {
      setSelectedRun(next.id);
      setDetail(await request<Run>(`/api/runs/${next.id}`));
    } else {
      setDetail(await request<Run>(`/api/runs/${cancelledId}`));
    }
    setActiveTrial(null);
    setTrialLog("");
    // 后端在 preflight 窗口或运行已结束时会拒绝取消，不能谎报清理已触发
    setNotice(
      outcome.cancelled
        ? "运行已取消，Docker 容器、网络和 Trial 镜像清理已触发"
        : "取消未生效：运行可能尚未启动或刚刚结束，请查看最新状态",
    );
  };
  const openTrial = async (trial: Trial) => {
    setActiveTrial(trial);
    setTrialLog("正在加载日志…");
    // 列表/SSE 数据不含 patch（体积原因），点击时按需拉取完整 Trial
    const [full, log] = await Promise.allSettled([
      request<Trial>(
        `/api/runs/${selectedRun}/trials/${encodeURIComponent(trial.id)}`,
      ),
      request<{ log: string }>(
        `/api/runs/${selectedRun}/trials/${encodeURIComponent(trial.id)}/log`,
      ),
    ]);
    if (full.status === "fulfilled") setActiveTrial(full.value);
    setTrialLog(
      log.status === "fulfilled" ? log.value.log : "该 Trial 暂无日志",
    );
  };
  const baseline = async () => {
    if (!detail) return;
    await request(`/api/runs/${detail.id}/baseline`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    setDetail(await request<Run>(`/api/runs/${detail.id}`));
    refreshRuns();
  };
  const resetBaseline = async () => {
    if (!detail) return;
    await request(`/api/runs/${detail.id}/baseline`, { method: "DELETE" });
    setDetail(await request<Run>(`/api/runs/${detail.id}`));
    refreshRuns();
    setNotice("已恢复 DeepSWE 官方基线");
  };
  const savePrefs = async () => {
    if (!prefs) return;
    setPrefs(
      await request<Prefs>("/api/settings", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(prefs),
      }),
    );
    setNotice("设置已保存");
  };
  const restore = async (file: File) => {
    try {
      const body = await file.text();
      const result = await request<{ restored: boolean; skipped_runs: number }>(
        "/api/restore",
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body,
        },
      );
      setNotice(
        result.skipped_runs
          ? `备份已恢复；${result.skipped_runs} 条含非法 job_name 的记录被跳过`
          : "备份已恢复",
      );
    } catch (e) {
      setNotice(`恢复失败：${String(e)}`);
    }
    refreshRuns();
  };
  const deleteResults = async () => {
    const ids = [...selectedResults];
    if (
      !ids.length ||
      !confirm(
        `删除选中的 ${ids.length} 条运行、日志和结果文件，并清理其未使用的 Trial Docker 镜像。\n共享基础镜像和构建缓存将保留。`,
      )
    )
      return;
    const failures: { id: number; message: string }[] = [];
    let removedImages = 0;
    let skippedImages = 0;
    for (const id of ids) {
      try {
        const body = await request<{
          docker_cleanup?: {
            removed_images: string[];
            skipped_images: { name: string }[];
          };
        }>(`/api/runs/${id}`, { method: "DELETE" });
        removedImages += body.docker_cleanup?.removed_images?.length ?? 0;
        skippedImages += body.docker_cleanup?.skipped_images?.length ?? 0;
      } catch (e) {
        failures.push({ id, message: String(e) });
      }
    }
    const failedIds = failures.map((x) => x.id);
    setSelectedResults(failedIds);
    if (
      selectedRun &&
      ids.includes(selectedRun) &&
      !failedIds.includes(selectedRun)
    ) {
      setSelectedRun(null);
      setDetail(null);
      setActiveTrial(null);
    }
    refreshRuns();
    const cleanupSummary = `，清理 ${removedImages} 个 Trial 镜像${
      skippedImages ? `；${skippedImages} 个镜像仍被使用，已跳过` : ""
    }`;
    setNotice(
      failures.length
        ? `${ids.length - failures.length} 条已删除${cleanupSummary}；${failures.length} 条失败：${failures[0].message}`
        : `已删除 ${ids.length} 条运行${cleanupSummary}`,
    );
  };
  if (!boot)
    return <main className="loading">正在连接 DeepSWE Regression Lab…</main>;
  const nav = [
    ["dashboard", Gauge],
    ["new run", Play],
    ["live", Wifi],
    ["results", ClipboardList],
    ["compare", BarChart3],
    ["tasks", FileCode2],
    ["settings", SettingsIcon],
    ["diagnostics", ShieldCheck],
  ] as const;
  return (
    <div className="shell">
      <aside>
        <div className="brand">
          <Box />{" "}
          <span>
            DeepSWE<small>REGRESSION LAB</small>
          </span>
        </div>
        <nav>
          {nav.map(([name, Icon]) => (
            <button
              key={name}
              className={tab === name ? "active" : ""}
              onClick={() => setTab(name)}
            >
              <Icon />
              {name}
            </button>
          ))}
        </nav>
        <div className="local">
          <ShieldCheck /> 仅本机访问
          <br />
          <span>127.0.0.1:8765</span>
        </div>
      </aside>
      <main>
        <header>
          <div>
            <p>DEEPSWE / {tab.toUpperCase()}</p>
            <h1>
              {
                (
                  {
                    dashboard: "系统概览",
                    "new run": "创建真实运行",
                    live: "实时进度",
                    results: "运行结果",
                    compare: "运行比较",
                    tasks: "任务目录",
                    settings: "设置与数据",
                    diagnostics: "环境诊断",
                  } as any
                )[tab]
              }
            </h1>
          </div>
          <button className="icon" onClick={refresh}>
            <RefreshCw />
          </button>
        </header>
        {notice && (
          <div className="notice" onClick={() => setNotice("")}>
            {notice}
          </div>
        )}
        {tab === "dashboard" && (
          <Dashboard latest={latest} checks={checks} openRun={openRun} />
        )}
        {tab === "new run" && (
          <section className="panel agentmode">
            <div>
              <h2>Agent 运行方式</h2>
              <p className="muted">
                “并发数”是每个 Agent 的 worker 数；三 Agent
                同跑会创建三条可独立查看和取消的运行。
              </p>
            </div>
            <label>
              <input
                type="radio"
                checked={!allAgents}
                onChange={() => setAllAgents(false)}
              />{" "}
              单 Agent
            </label>
            <label>
              <input
                type="radio"
                checked={allAgents}
                onChange={() => setAllAgents(true)}
              />{" "}
              三 Agent 同时跑
            </label>
            <b>
              {allAgents
                ? `总 worker 上限 ${boot.agents.length * concurrency}`
                : `${concurrency} workers`}
            </b>
          </section>
        )}
        {tab === "new run" && (
          <section className="panel form">
            <div className="panelhead">
              <div>
                <h2>运行配置</h2>
                <p>所有选择都会记录在本次运行中，便于严格复现。</p>
              </div>
              <b>
                {selectedTasks.length *
                  (Number.isFinite(attemptsNum) ? attemptsNum : 0)}{" "}
                TRIALS
              </b>
            </div>
            <div className="formgrid">
              <label>
                Agent
                <select
                  value={agent}
                  onChange={(e) => setAgent(e.target.value)}
                >
                  {boot.agents.map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </select>
              </label>
              <label>
                模型
                <select
                  value={model}
                  onChange={(e) => setModel(e.target.value)}
                >
                  {boot.models.map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </select>
              </label>
              <label>
                Reasoning effort
                <select
                  value={effort}
                  onChange={(e) => setEffort(e.target.value)}
                >
                  {boot.efforts.map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </select>
              </label>
              <label>
                Service tier
                <select value={tier} onChange={(e) => setTier(e.target.value)}>
                  {boot.service_tiers.map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </select>
              </label>
              <label>
                每题次数
                <input
                  type="number"
                  min="1"
                  max="10"
                  value={attempts}
                  onChange={(e) => setAttempts(e.target.value)}
                />
              </label>
              <label>
                并发数
                <select
                  value={concurrency}
                  onChange={(e) => setConcurrency(+e.target.value)}
                >
                  {boot.setting_options.concurrency.map((x) => (
                    <option key={x}>{x}</option>
                  ))}
                </select>
                <small className={`risk r${concurrency}`}>
                  {concurrency <= 2
                    ? "安全"
                    : concurrency === 3
                      ? "内存压力警告"
                      : "需要高并发确认"}
                </small>
              </label>
              <label>
                Agent 超时（秒）
                <input
                  type="number"
                  min="60"
                  value={agentTimeout}
                  onChange={(e) => setAgentTimeout(e.target.value)}
                />
              </label>
              <label>
                Verifier 超时（秒）
                <input
                  type="number"
                  min="60"
                  value={verifierTimeout}
                  onChange={(e) => setVerifierTimeout(e.target.value)}
                />
              </label>
              <label>
                基础设施重试次数
                <input
                  type="number"
                  min="0"
                  max="6"
                  value={infrastructureMaxRetries}
                  onChange={(e) => setInfrastructureMaxRetries(e.target.value)}
                />
              </label>
              {(agent === "claude-code" || allAgents) && (
                <label>
                  Claude 最大轮数
                  <input
                    type="number"
                    min="20"
                    max="200"
                    value={claudeMaxTurns}
                    onChange={(e) => setClaudeMaxTurns(e.target.value)}
                  />
                </label>
              )}
              {(agent === "codex" || allAgents) && (
                <>
                  <label>
                    Codex HTTP 重试次数
                    <input
                      type="number"
                      min="0"
                      max="10"
                      value={codexRequestMaxRetries}
                      onChange={(e) => setCodexRequestMaxRetries(e.target.value)}
                    />
                  </label>
                  <label>
                    Codex 流重试次数
                    <input
                      type="number"
                      min="0"
                      max="10"
                      value={codexStreamMaxRetries}
                      onChange={(e) => setCodexStreamMaxRetries(e.target.value)}
                    />
                  </label>
                  <label>
                    Codex 流空闲超时（秒）
                    <input
                      type="number"
                      min="30"
                      max="1800"
                      value={codexStreamIdleTimeout}
                      onChange={(e) => setCodexStreamIdleTimeout(e.target.value)}
                    />
                  </label>
                </>
              )}
            </div>
            <div className="toggles">
              <label>
                <input
                  type="checkbox"
                  checked={retry}
                  onChange={(e) => setRetry(e.target.checked)}
                />
                基础设施错误自动重试（仅限已识别的 429/5xx/连接中断/API 超时）
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={verification}
                  onChange={(e) => setVerification(e.target.checked)}
                />
                启用 Verifier
              </label>
            </div>
            <div className="tasktitle">
              <div>
                <h3>{boot.task_suite.name}</h3>
                <span>{selectedTasks.length} selected</span>
              </div>
              <button disabled={!selectedTasks.length} onClick={() => setSelectedTasks([])}>清空</button>
            </div>
            {!selectedTasks.length && <div className="emptytasklink">未选择任务。<button className="textbutton" onClick={() => setTab("tasks")}>前往任务目录选择</button></div>}
            <div className="tasks">
              {boot.task_suite.tasks.map((t) => (
                <label key={t.id} className={!t.available ? "disabled" : ""}>
                  <input
                    type="checkbox"
                    checked={selectedTasks.includes(t.id)}
                    disabled={!t.available}
                    onChange={(e) =>
                      setSelectedTasks(
                        e.target.checked
                          ? [...selectedTasks, t.id]
                          : selectedTasks.filter((x) => x !== t.id),
                      )
                    }
                  />
                  <span className="taskidentity">
                    <b>{t.suite_id}</b>
                    <span>{t.title}</span>
                    <small>
                      目录：{t.id}
                      {t.external_id ? ` · 数据集 ID：${t.external_id}` : ""}
                      {t.official_pass_rate != null
                        ? ` · 官方 ${Math.round(t.official_pass_rate * 100)}% · ${duration(t.official_avg_duration_seconds)}`
                        : ""}
                    </small>
                  </span>
                  <i>{t.available ? "READY" : "MISSING"}</i>
                </label>
              ))}
              {selectedTasks
                .filter((id) => !boot.task_suite.tasks.some((t) => t.id === id))
                .map((id) => {
                  const task = tasks.find((item) => item.id === id);
                  return (
                  <label key={id}>
                    <input
                      type="checkbox"
                      checked
                      onChange={() =>
                        setSelectedTasks(selectedTasks.filter((x) => x !== id))
                      }
                    />
                    <span className="taskidentity">
                      <b>{task?.code || "TASK-UNKNOWN"}</b>
                      <span>{task?.title || id}</span>
                      <small>目录：{id}</small>
                    </span>
                    <i>{task ? "READY" : "CUSTOM"}</i>
                  </label>
                  );
                })}
            </div>
            <button
              className="primary"
              disabled={!selectedTasks.length || starting}
              onClick={start}
            >
              <Play /> {starting ? "正在创建…" : "创建真实运行"}
            </button>
          </section>
        )}
        {tab === "live" && (
          <Live
            runs={runs}
            detail={detail && !terminal.has(detail.status) ? detail : null}
            selectRun={(id) => {
              // 切换 Agent 视图时清掉上一个运行的 Trial 详情，防止 A 的 patch 挂在 B 下
              setSelectedRun(id);
              setActiveTrial(null);
              setTrialLog("");
            }}
            cancel={cancelCurrent}
            openTrial={openTrial}
            activeTrial={activeTrial}
            trialLog={trialLog}
          />
        )}
        {tab === "results" && (
          <Results
            runs={runs}
            detail={detail}
            openRun={openRun}
            openTrial={openTrial}
            activeTrial={activeTrial}
            trialLog={trialLog}
            baseline={baseline}
            resetBaseline={resetBaseline}
            selected={selectedResults}
            setSelected={setSelectedResults}
            remove={deleteResults}
          />
        )}
        {tab === "compare" && (
          <Compare
            runs={runs}
            selected={compareIds}
            setSelected={setCompareIds}
            comparison={comparison}
          />
        )}
        {tab === "tasks" && (
          <Tasks
            items={tasks}
            selected={selectedTasks}
            setSelected={setSelectedTasks}
            create={() => setTab("new run")}
            notify={setNotice}
            reload={() => request<TaskInfo[]>("/api/tasks").then(setTasks)}
          />
        )}
        {tab === "settings" && (
          <Settings
            prefs={prefs}
            setPrefs={setPrefs}
            save={savePrefs}
            restore={restore}
            boot={boot}
            notify={setNotice}
          />
        )}
        {tab === "diagnostics" && <Diagnostics checks={checks} />}
      </main>
    </div>
  );
}

function Dashboard({
  latest,
  checks,
  openRun,
}: {
  latest?: Run;
  checks: Check[];
  openRun: (id: number) => void;
}) {
  const ready = checks.filter((c) => c.status === "ok").length;
  return (
    <>
      <section className="hero">
        <div>
          <Activity />
          <span>三 Agent 真实回归链路</span>
          <strong>{latest?.status.toUpperCase() || "READY"}</strong>
          <p>
            mini-swe-agent、Codex 与 Claude Code 使用同一套任务、Verifier
            和成本归一化。
          </p>
          {latest && (
            <button className="textbutton" onClick={() => openRun(latest.id)}>
              查看最近运行 →
            </button>
          )}
        </div>
        <div className="healthring">
          <b>
            {ready}/{checks.length}
          </b>
          <span>环境检查通过</span>
        </div>
      </section>
      <section className="metrics">
        {" "}
        <Metric label="最近 Reward" value={latest?.reward ?? "—"} />
        <Metric label="报告费用" value={money(latest?.reported_cost_usd)} />
        <Metric label="Agent" value={latest?.agent || "—"} />
        <Metric label="任务数" value={latest?.tasks?.length ?? "—"} />
      </section>
      <Diagnostics checks={checks} />
    </>
  );
}
function Live({
  runs,
  detail,
  selectRun,
  cancel,
  openTrial,
  activeTrial,
  trialLog,
}: {
  runs: Run[];
  detail: Run | null;
  selectRun: (id: number) => void;
  cancel: () => void;
  openTrial: (t: Trial) => void;
  activeTrial: Trial | null;
  trialLog: string;
}) {
  if (!detail) return <Empty text="创建或选择一次运行后，这里显示实时进度。" />;
  const active = runs.filter((r) => !terminal.has(r.status));
  return (
    <>
      {active.length > 1 && (
        <section className="panel liveswitch">
          <b>正在运行 {active.length} 个 Agent</b>
          {active.map((r) => (
            <button
              className={r.id === detail.id ? "selected" : ""}
              key={r.id}
              onClick={() => selectRun(r.id)}
            >
              <Status value={r.status} />
              {r.agent}
              <small>{r.run_code || `RUN-${String(r.id).padStart(6, "0")}`}</small>
            </button>
          ))}
        </section>
      )}
      <section className="panel">
        <div className="panelhead">
          <div>
            <Status value={detail.status} />
            <h2>{detail.run_code || `RUN-${String(detail.id).padStart(6, "0")}`}</h2>
            <p>
              {detail.agent} · {detail.model} · {detail.reasoning_effort} ·
              concurrency {detail.concurrency}
            </p>
            <code className="technicalid">Pier Job：{detail.job_name}</code>
          </div>
          {!terminal.has(detail.status) && (
            <button className="danger" onClick={cancel}>
              <Square />
              取消运行
            </button>
          )}
        </div>
        {detail.error && (
          <div className="runerror" role="alert">
            <b>失败原因</b>
            <span>{detail.error}</span>
          </div>
        )}
        <div className="progress">
          <span style={{ width: `${detail.progress?.percent || 0}%` }} />
        </div>
        <div className="progressmeta">
          <b>
            {detail.progress?.completed || 0}/{detail.progress?.total || 0}{" "}
            complete
          </b>
          <span>当前阶段：{detail.stage}</span>
        </div>
      </section>
      <section className="trialgrid">
        {detail.trials?.map((t) => (
          <button
            className={activeTrial?.id === t.id ? "selected" : ""}
            key={t.id}
            onClick={() => openTrial(t)}
          >
            <Status value={t.status} />
            <small className="identitycode">{t.trial_code}</small>
            <b>{t.task_title || t.task}</b>
            <code>{t.task_slug || t.task}</code>
            <span>
              reward {t.reward ?? "—"} · {duration(t.duration_seconds)}
            </span>
          </button>
        ))}
      </section>
      {activeTrial && <TrialDetail trial={activeTrial} log={trialLog} />}
    </>
  );
}
function Results({
  runs,
  detail,
  openRun,
  openTrial,
  activeTrial,
  trialLog,
  baseline,
  resetBaseline,
  selected,
  setSelected,
  remove,
}: {
  runs: Run[];
  detail: Run | null;
  openRun: (id: number) => void;
  openTrial: (t: Trial) => void;
  activeTrial: Trial | null;
  trialLog: string;
  baseline: () => void;
  resetBaseline: () => void;
  selected: number[];
  setSelected: (ids: number[]) => void;
  remove: () => void;
}) {
  const all = runs.length > 0 && selected.length === runs.length;
  return (
    <div className="resultsLayout">
      <section className="panel runlist">
        <div className="resulttools">
          <label>
            <input
              type="checkbox"
              checked={all}
              onChange={() => setSelected(all ? [] : runs.map((r) => r.id))}
            />
            全选
          </label>
          <b>Job 历史</b>
          <button
            className="danger"
            disabled={!selected.length}
            onClick={remove}
          >
            <Trash2 />
            删除 {selected.length || ""}
          </button>
        </div>
        {runs.length ? (
          runs.map((r) => (
            <div
              className={`runrow ${detail?.id === r.id ? "selected" : ""}`}
              key={r.id}
            >
              <input
                type="checkbox"
                checked={selected.includes(r.id)}
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? [...selected, r.id]
                      : selected.filter((id) => id !== r.id),
                  )
                }
              />
              <button onClick={() => openRun(r.id)}>
                <Status value={r.status} />
                <b>{r.agent}</b>
                <span>
                  {r.run_code || `RUN-${String(r.id).padStart(6, "0")}`}
                  <small>{r.job_name}</small>
                </span>
                <em>{r.reward == null ? "—" : r.reward.toFixed(3)}</em>
              </button>
            </div>
          ))
        ) : (
          <Empty text="暂无运行" />
        )}
      </section>
      <div>
        {detail ? (
          <>
            <section className="metrics">
              <Metric label="Reward" value={detail.reward ?? "—"} />
              <Metric
                label="通过"
                value={`${detail.progress?.passed || 0}/${detail.progress?.total || 0}`}
              />
              <Metric
                label="Input / Cache"
                value={`${number(detail.input_tokens)} / ${number(detail.cached_tokens)}`}
              />
              <Metric label="Output" value={number(detail.output_tokens)} />
              <Metric
                label="估算费用"
                value={money(detail.estimated_cost_usd)}
              />
            </section>
            <section className="runmeta">
              <span><b>Agent</b>{detail.agent}</span>
              <span><b>模型</b>{detail.model}</span>
              <span><b>思考强度</b>{detail.reasoning_effort}</span>
              <span><b>并发</b>{detail.concurrency}</span>
            </section>
            {detail.error && (
              <div className="runerror" role="alert">
                <b>失败原因</b>
                <span>{detail.error}</span>
              </div>
            )}
            {detail.regression && (
              <div className={`regression ${detail.regression.level}`}>
                <b>基线：{detail.regression.baseline_name}</b>
                <span>
                  通过率 {percent(detail.regression.current_pass_rate)} vs {percent(detail.regression.baseline_pass_rate)}
                  {detail.regression.pass_rate_delta != null && `（${detail.regression.pass_rate_delta >= 0 ? "+" : ""}${(detail.regression.pass_rate_delta * 100).toFixed(2)}pp）`}
                  {detail.regression.current_duration_seconds != null && detail.regression.baseline_duration_seconds != null &&
                    ` · 耗时 ${duration(detail.regression.current_duration_seconds)} vs ${duration(detail.regression.baseline_duration_seconds)}`}
                  {detail.regression.baseline_trials ? ` · 官方 ${detail.regression.baseline_trials} 次 Trial` : ""}
                  {detail.regression.reasons.length ? ` · ${detail.regression.reasons.join("；")}` : " · 未检测到回归"}
                </span>
              </div>
            )}
            <section className="panel">
              <div className="panelhead">
                <h2>Trial 结果</h2>
              </div>
              <div className="table">
                <div className="tr">
                  <b>Task</b>
                  <b>Status</b>
                  <b>Reward</b>
                  <b>F2P / P2P</b>
                  <b>Duration</b>
                  <b>Steps</b>
                </div>
                {detail.trials?.map((t) => (
                  <button
                    className="tr"
                    key={t.id}
                    onClick={() => openTrial(t)}
                  >
                    <span className="tabletask">
                      <b>{t.task_code}</b>
                      <span>{t.task_title || t.task}</span>
                      <small>{t.task_slug || t.task}</small>
                    </span>
                    <Status value={t.status} />
                    <span>{t.reward ?? "—"}</span>
                    <span>
                      {t.f2p ?? "—"} / {t.p2p ?? "—"}
                    </span>
                    <span>{duration(t.duration_seconds)}</span>
                    <span>{t.steps ?? "—"}</span>
                  </button>
                ))}
              </div>
            </section>
            {activeTrial && <TrialDetail trial={activeTrial} log={trialLog} />}
          </>
        ) : (
          <Empty text="从左侧选择一次运行查看详情。" />
        )}
      </div>
    </div>
  );
}
function TrialDetail({ trial, log }: { trial: Trial; log: string }) {
  return (
    <section className="panel trialdetail">
      <span className="identitycode">{trial.trial_code}</span>
      <h2>{trial.task_title || trial.task}</h2>
      <p className="technicalid">标准 Task：{trial.task_code} · {trial.task_slug || trial.task}</p>
      {trial.resource_name && (
        <p className="technicalid">Pier / Docker 资源前缀：{trial.resource_name}</p>
      )}
      <div className="metrics compact">
        <Metric label="Partial" value={trial.partial ?? "—"} />
        <Metric label="Tokens" value={number(trial.input_tokens)} />
        <Metric label="Cost" value={money(trial.reported_cost_usd)} />
        <Metric label="Patch" value={`${number(trial.patch_bytes)} B`} />
      </div>
      {trial.failure_message && (
        <pre className="failure">
          {trial.failure_type}: {trial.failure_message}
        </pre>
      )}
      <div className="split">
        <div>
          <h3>Patch</h3>
          <pre>{trial.patch || "暂无 Patch"}</pre>
        </div>
        <div>
          <h3>日志</h3>
          <pre>{log}</pre>
        </div>
      </div>
    </section>
  );
}
function Compare({
  runs,
  selected,
  setSelected,
  comparison,
}: {
  runs: Run[];
  selected: string[];
  setSelected: (x: string[]) => void;
  comparison: any;
}) {
  const [taskFilter, setTaskFilter] = useState("all");
  const [agentFilter, setAgentFilter] = useState("all");
  const [modelFilter, setModelFilter] = useState("all");
  const [effortFilter, setEffortFilter] = useState("all");
  const taskNames = [...new Set(runs.flatMap((run) => run.tasks))].sort();
  const filteredRuns = runs.filter((run) =>
    (taskFilter === "all" || run.tasks.includes(taskFilter)) &&
    (agentFilter === "all" || run.agent === agentFilter) &&
    (modelFilter === "all" || run.model === modelFilter) &&
    (effortFilter === "all" || run.reasoning_effort === effortFilter));
  const groupedRuns = taskNames.filter((task) => taskFilter === "all" || task === taskFilter).map((task) => ({task, runs: filteredRuns.filter((run) => run.tasks.includes(task))})).filter((group) => group.runs.length);
  return (
    <>
      <section className="panel">
        <div className="panelhead">
          <div>
            <h2>选择结果</h2>
            <p>最多选择 8 个 Run × Task 结果；同一个 Run 中的不同任务可以独立勾选。</p>
          </div>
          <b>{selected.length}/8</b>
        </div>
        <div className="comparefilters">
          <label>Task<select value={taskFilter} onChange={(e)=>setTaskFilter(e.target.value)}><option value="all">全部任务</option>{taskNames.map(x=><option key={x} value={x}>{x}</option>)}</select></label>
          <label>Agent<select value={agentFilter} onChange={(e)=>setAgentFilter(e.target.value)}><option value="all">全部 Agent</option>{[...new Set(runs.map(x=>x.agent))].map(x=><option key={x}>{x}</option>)}</select></label>
          <label>模型<select value={modelFilter} onChange={(e)=>setModelFilter(e.target.value)}><option value="all">全部模型</option>{[...new Set(runs.map(x=>x.model))].map(x=><option key={x}>{x}</option>)}</select></label>
          <label>思考强度<select value={effortFilter} onChange={(e)=>setEffortFilter(e.target.value)}><option value="all">全部强度</option>{[...new Set(runs.map(x=>x.reasoning_effort))].map(x=><option key={x}>{x}</option>)}</select></label>
        </div>
        {groupedRuns.map(group=><div className="comparegroup" key={group.task}><h3>{group.task}</h3><div className="comparepick">
          {group.runs.map((r) => { const selectionKey = `${r.id}:${group.task}`; return (
            <label key={selectionKey}>
              <input
                type="checkbox"
                checked={selected.includes(selectionKey)}
                disabled={!selected.includes(selectionKey) && selected.length >= 8}
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? [...selected, selectionKey]
                      : selected.filter((x) => x !== selectionKey),
                  )
                }
              />
              <Status value={r.status} />
              <span>
                {r.agent} · {r.model} · {r.reasoning_effort}
              </span>
              <em>
                <b>{r.run_code || `RUN-${String(r.id).padStart(6, "0")}`}</b>
                <small>{r.job_name}</small>
              </em>
            </label>
          )})}
        </div></div>)}
      </section>
      {comparison?.runs?.length > 0 && (
        <>
          <section className="metrics">
            {comparison.runs.map((r: Run) => (
              <Metric
                key={r.id}
                label={`${r.run_code || `RUN-${String(r.id).padStart(6, "0")}`} ${r.agent}`}
                value={`${r.progress?.passed || 0}/${r.progress?.total || 0}`}
                hint={`${money(r.estimated_cost_usd)} · reward ${r.reward ?? "—"}`}
              />
            ))}
          </section>
          <section className="panel">
            <h2>同任务详细对比</h2>
            {!comparison.tasks.length ? <Empty text="所选运行没有共同任务。" /> : comparison.tasks.map((row: any) => (
              <div className="comparetask" key={row.task}>
                <h3>{row.task_code} · {row.task_title}</h3>
                <div className="comparetable">
                  <b>运行 / 基线</b><b>通过率</b><b>耗时</b><b>Input / Cache / Output</b><b>成本</b><b>步骤</b>
                  <span>DeepSWE 官方</span><span>{row.official?.pass_rate == null ? "—" : `${Math.round(row.official.pass_rate * 100)}%`}</span><span>{duration(row.official?.avg_duration_seconds)}</span><span>{number(row.official?.avg_input_tokens)} / {number(row.official?.avg_cache_tokens)} / {number(row.official?.avg_output_tokens)}</span><span>{money(row.official?.avg_cost_usd)}</span><span>{row.official?.avg_steps ?? "—"}</span>
                  {comparison.runs.filter((r: Run) => Object.hasOwn(row.runs, String(r.id))).map((r: Run) => { const v=row.runs[String(r.id)] || {}; return <React.Fragment key={r.id}>
                    <span><b>{r.agent}</b><small>{r.model} · {r.reasoning_effort}</small></span>
                    <span>{v.pass_rate == null ? "—" : `${Math.round(v.pass_rate * 100)}%`}</span><span>{duration(v.duration_seconds)}</span>
                    <span>{number(v.input_tokens)} / {number(v.cached_tokens)} / {number(v.output_tokens)}</span><span>{money(v.cost_usd)}</span><span>{v.steps ?? "—"}</span>
                  </React.Fragment>})}
                </div>
              </div>
            ))}
          </section>
        </>
      )}
    </>
  );
}
function Tasks({
  items,
  selected,
  setSelected,
  create,
  notify,
  reload,
}: {
  items: TaskInfo[];
  selected: string[];
  setSelected: (x: string[]) => void;
  create: () => void;
  notify: (message: string) => void;
  reload: () => void;
}) {
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState("all");
  const [category, setCategory] = useState("all");
  const [dataFilter, setDataFilter] = useState("all");
  const [sortBy, setSortBy] = useState("number-asc");
  const [syncing, setSyncing] = useState(false);
  const languages = useMemo(
    () => [...new Set(items.map((t) => t.language || "unknown"))].sort(),
    [items],
  );
  const categories = useMemo(
    () => [...new Set(items.map((t) => t.category || "task"))].sort(),
    [items],
  );
  const visible = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const filtered = items.filter((t) => {
      const matchesQuery = !needle ||
        [t.code, t.id, t.title, t.description, t.language, t.category]
          .filter(Boolean)
          .join(" ")
          .toLowerCase()
          .includes(needle);
      const matchesLanguage = language === "all" || (t.language || "unknown") === language;
      const matchesCategory = category === "all" || (t.category || "task") === category;
      const matchesData =
        dataFilter === "all" ||
        (dataFilter === "official" && t.official_pass_rate != null) ||
        (dataFilter === "local" && t.local_trials > 0) ||
        (dataFilter === "selected" && selected.includes(t.id));
      return matchesQuery && matchesLanguage && matchesCategory && matchesData;
    });
    const nullLast = (value: number | null | undefined, fallback: number) => value == null ? fallback : value;
    return [...filtered].sort((a, b) => {
      if (sortBy === "pass-desc") return nullLast(b.official_pass_rate, -1) - nullLast(a.official_pass_rate, -1);
      if (sortBy === "pass-asc") return nullLast(a.official_pass_rate, 2) - nullLast(b.official_pass_rate, 2);
      if (sortBy === "duration-asc") return nullLast(a.official_avg_duration_seconds, Infinity) - nullLast(b.official_avg_duration_seconds, Infinity);
      if (sortBy === "duration-desc") return nullLast(b.official_avg_duration_seconds, -1) - nullLast(a.official_avg_duration_seconds, -1);
      if (sortBy === "local-desc") return nullLast(b.local_pass_rate, -1) - nullLast(a.local_pass_rate, -1);
      if (sortBy === "title-asc") return a.title.localeCompare(b.title);
      return a.task_number - b.task_number;
    });
  }, [items, query, language, category, dataFilter, sortBy, selected]);
  const visibleIds = visible.map((t) => t.id);
  const allVisibleSelected = visibleIds.length > 0 && visibleIds.every((id) => selected.includes(id));
  const resetFilters = () => {
    setQuery("");
    setLanguage("all");
    setCategory("all");
    setDataFilter("all");
    setSortBy("number-asc");
  };
  const syncOfficial = async () => {
    setSyncing(true);
    try {
      const result = await request<{ n_tasks: number; n_trials: number }>(
        "/api/tasks/sync-official",
        { method: "POST" },
      );
      notify(
        `官方统计已同步：${result.n_tasks} 个任务 · ${result.n_trials} 条 trial`,
      );
      reload();
    } catch (e) {
      notify(`同步失败：${String(e)}`);
    }
    setSyncing(false);
  };
  return (
    <>
      <section className="panel tasktools">
        <div>
          <h2>任务库</h2>
          <p className="muted">
            官方通过率与平均耗时来自 DeepSWE {""}
            全模型全档位聚合，可作为挑选任务的快速依据。
          </p>
        </div>
        <input
          className="tasksearch"
          placeholder="搜索任务、语言或标题"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        <button className="secondary" disabled={syncing} onClick={syncOfficial}>
          <RefreshCw />
          {syncing ? "同步中…" : "同步官方统计"}
        </button>
        <button
          className="primary"
          disabled={!selected.length}
          onClick={create}
        >
          <Play />
          使用已选 {selected.length} 题创建运行
        </button>
      </section>
      <section className="panel taskfilters">
        <div className="filterfield">
          <label>语言</label>
          <select value={language} onChange={(e) => setLanguage(e.target.value)}>
            <option value="all">全部语言</option>
            {languages.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </div>
        <div className="filterfield">
          <label>任务类型</label>
          <select value={category} onChange={(e) => setCategory(e.target.value)}>
            <option value="all">全部类型</option>
            {categories.map((value) => <option key={value} value={value}>{value}</option>)}
          </select>
        </div>
        <div className="filterfield">
          <label>数据状态</label>
          <select value={dataFilter} onChange={(e) => setDataFilter(e.target.value)}>
            <option value="all">全部任务</option>
            <option value="official">有官方数据</option>
            <option value="local">有本地结果</option>
            <option value="selected">仅已选择</option>
          </select>
        </div>
        <div className="filterfield">
          <label>排序</label>
          <select value={sortBy} onChange={(e) => setSortBy(e.target.value)}>
            <option value="number-asc">编号升序</option>
            <option value="pass-desc">官方通过率：高到低</option>
            <option value="pass-asc">官方通过率：低到高</option>
            <option value="duration-asc">平均执行时间：短到长</option>
            <option value="duration-desc">平均执行时间：长到短</option>
            <option value="local-desc">本地通过率：高到低</option>
            <option value="title-asc">标题 A–Z</option>
          </select>
        </div>
        <div className="filteractions">
          <span>显示 {visible.length} / {items.length}</span>
          <button
            className="secondary"
            disabled={!visibleIds.length}
            onClick={() =>
              setSelected(
                allVisibleSelected
                  ? selected.filter((id) => !visibleIds.includes(id))
                  : [...new Set([...selected, ...visibleIds])],
              )
            }
          >
            {allVisibleSelected ? "取消当前结果" : "选择当前结果"}
          </button>
          <button className="textbutton" onClick={resetFilters}>重置</button>
        </div>
      </section>
      <section className="cards">
        {visible.map((t) => (
          <article
            key={t.id}
            className={selected.includes(t.id) ? "picked" : ""}
          >
            <label className="taskcheck">
              <input
                type="checkbox"
                checked={selected.includes(t.id)}
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? [...new Set([...selected, t.id])]
                      : selected.filter((x) => x !== t.id),
                  )
                }
              />
              {selected.includes(t.id) ? "已选择" : "选择任务"}
            </label>
            <div>
              <span><b className="taskcode">{t.code}</b>{t.language || "unknown"}</span>
              <i>{t.category || "task"}</i>
            </div>
            <h2>{t.title}</h2>
            <code className="taskslug">{t.id}</code>
            <p>{t.description}</p>
            <footer>
              <b>
                {t.official_pass_rate != null
                  ? `官方 ${Math.round(t.official_pass_rate * 100)}% · ${duration(t.official_avg_duration_seconds)}`
                  : "无官方数据"}
              </b>
              <em>
                {t.local_trials
                  ? `本地 ${t.local_trials} trials · ${
                      t.local_pass_rate == null
                        ? "—"
                        : `${Math.round(t.local_pass_rate * 100)}%`
                    }`
                  : "无本地历史"}
              </em>
            </footer>
            {t.last_failure && <small>最近失败：{t.last_failure}</small>}
          </article>
        ))}
        {!visible.length && (
          <div className="empty taskempty">没有符合当前筛选条件的任务。</div>
        )}
      </section>
    </>
  );
}
function Settings({
  prefs,
  setPrefs,
  save,
  restore,
  boot,
  notify,
}: {
  prefs?: Prefs;
  setPrefs: (p: Prefs) => void;
  save: () => void;
  restore: (f: File) => void;
  boot: Boot;
  notify: (message: string) => void;
}) {
  if (!prefs) return <Empty text="正在读取设置…" />;
  return (
    <>
      <section className="panel form">
        <h2>本机设置</h2>
        <div className="formgrid">
          <label>
            凭据文件
            <select
              value={prefs.credential_file}
              onChange={(e) =>
                setPrefs({ ...prefs, credential_file: e.target.value })
              }
            >
              {boot.setting_options.credential_files.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label>
            Jobs 目录
            <select
              value={prefs.jobs_dir}
              onChange={(e) => setPrefs({ ...prefs, jobs_dir: e.target.value })}
            >
              {boot.setting_options.jobs_dirs.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label>
            默认 Agent
            <select
              value={prefs.default_agent}
              onChange={(e) =>
                setPrefs({ ...prefs, default_agent: e.target.value })
              }
            >
              {boot.agents.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label>
            默认模型
            <select
              value={prefs.default_model}
              onChange={(e) =>
                setPrefs({ ...prefs, default_model: e.target.value })
              }
            >
              {boot.models.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label>
            默认 Effort
            <select
              value={prefs.default_effort}
              onChange={(e) =>
                setPrefs({ ...prefs, default_effort: e.target.value })
              }
            >
              {boot.efforts.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label>
            默认并发
            <select
              value={prefs.default_concurrency}
              onChange={(e) =>
                setPrefs({ ...prefs, default_concurrency: +e.target.value })
              }
            >
              {boot.setting_options.concurrency.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label>
            单次 Run 费用熔断 (USD，0 禁用)
            <input
              type="number"
              min={0}
              step={0.5}
              value={prefs.run_budget_usd}
              onChange={(e) =>
                setPrefs({ ...prefs, run_budget_usd: +e.target.value })
              }
            />
          </label>
        </div>
        <button className="primary" onClick={save}>
          <Save />
          保存设置
        </button>
      </section>
      <section className="panel">
        <h2>导出、备份与恢复</h2>
        <div className="actions">
          <a className="secondary" href={apiBase + "/api/export.csv"}>
            <Download />
            导出 CSV
          </a>
          <a className="secondary" href={apiBase + "/api/export.json"}>
            <Database />
            下载 JSON 备份
          </a>
          <label className="secondary">
            <Upload />
            恢复 JSON
            <input
              type="file"
              accept="application/json"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0];
                if (file) restore(file);
                e.target.value = ""; // 重置以允许再次选择同名文件
              }}
            />
          </label>
        </div>
        <p className="muted">
          备份包含设置、运行索引和基线；Token 与完整凭据不会进入备份。
        </p>
      </section>
      <DockerCard prefs={prefs} setPrefs={setPrefs} notify={notify} />
    </>
  );
}
const formatBytes = (value: number | null | undefined) => {
  if (value == null) return "未知";
  if (value >= 1e9) return `${(value / 1e9).toFixed(2)} GB`;
  if (value >= 1e6) return `${(value / 1e6).toFixed(1)} MB`;
  return `${Math.round(value / 1e3)} KB`;
};
function DockerCard({
  prefs,
  setPrefs,
  notify,
}: {
  prefs: Prefs;
  setPrefs: (p: Prefs) => void;
  notify: (message: string) => void;
}) {
  const [storage, setStorage] = useState<DockerStorage>();
  const [busy, setBusy] = useState(false);
  const [scan, setScan] = useState<{
    imageCount: number;
    reclaimable: number | null;
  } | null>(null);
  const load = () =>
    request<DockerStorage>("/api/docker/storage")
      .then(setStorage)
      .catch((e) =>
        setStorage({ available: false, error: String(e), active_runs: 0 }),
      );
  useEffect(() => {
    load();
  }, []);
  const preview = (scope: string) =>
    request<any>("/api/docker/cleanup/preview", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        scope,
        retention_hours: prefs.docker_cache_retention_hours,
      }),
    });
  const cleanup = (scope: string) =>
    request<any>("/api/docker/cleanup", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        scope,
        retention_hours: prefs.docker_cache_retention_hours,
      }),
    });
  const scanResources = async () => {
    setBusy(true);
    try {
      const [expired, orphaned] = await Promise.all([
        preview("expired"),
        preview("orphaned"),
      ]);
      const imageCount =
        (expired.image_count ?? 0) + (orphaned.image_count ?? 0);
      const reclaimable =
        (expired.reclaimable_bytes ?? 0) + (orphaned.reclaimable_bytes ?? 0) ||
        null;
      setScan({ imageCount, reclaimable });
      notify(
        imageCount
          ? `发现 ${imageCount} 个可清理的 Trial 镜像条目`
          : "没有可清理的 Trial 镜像",
      );
    } catch (e) {
      notify(`扫描失败：${String(e)}`);
    }
    setBusy(false);
  };
  const cleanImages = async () => {
    setBusy(true);
    try {
      const [expired, orphaned] = await Promise.all([
        preview("expired"),
        preview("orphaned"),
      ]);
      const imageCount =
        (expired.image_count ?? 0) + (orphaned.image_count ?? 0);
      const reclaimable =
        (expired.reclaimable_bytes ?? 0) + (orphaned.reclaimable_bytes ?? 0);
      if (!imageCount) {
        notify("没有可清理的 Trial 镜像");
        setBusy(false);
        return;
      }
      if (
        !confirm(
          `将删除 ${imageCount} 个 Trial 镜像条目\n` +
            `预计释放的独占镜像空间：${reclaimable ? formatBytes(reclaimable) : "未知（以 Docker 实际结果为准）"}\n` +
            "不会删除任务基础镜像和构建缓存",
        )
      ) {
        setBusy(false);
        return;
      }
      const [expiredResult, orphanedResult] = [
        await cleanup("expired"),
        await cleanup("orphaned"),
      ];
      const removed =
        (expiredResult.removed_images?.length ?? 0) +
        (orphanedResult.removed_images?.length ?? 0);
      const skipped =
        (expiredResult.skipped_images?.length ?? 0) +
        (orphanedResult.skipped_images?.length ?? 0);
      notify(
        `已清理 ${removed} 个 Trial 镜像${skipped ? `；${skipped} 个仍被使用，已跳过` : ""}`,
      );
      setScan(null);
      load();
    } catch (e) {
      notify(`清理失败：${String(e)}`);
    }
    setBusy(false);
  };
  const cleanCache = async (all = false) => {
    setBusy(true);
    try {
      const retention = all ? 0 : prefs.docker_cache_retention_hours;
      const info = await request<any>("/api/docker/cleanup/preview", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({scope:"build_cache",retention_hours:retention})});
      if (!info.available) {
        notify(`Docker 不可用：${info.error ?? ""}`);
        setBusy(false);
        return;
      }
      if (
        !confirm(
          (all ? "将清空全部 Docker 构建缓存\n" : `将清理超过 ${retention} 小时（${Math.round(retention / 24)} 天）未使用的构建缓存\n`) +
            `当前缓存占用：${formatBytes(info.build_cache?.size_bytes)}，其中可回收：${formatBytes(info.build_cache?.reclaimable_bytes)}\n` +
            (all ? "下次运行可能需要重新构建镜像" : "近期缓存将保留，下次运行仍可命中"),
        )
      ) {
        setBusy(false);
        return;
      }
      const result = await request<any>("/api/docker/cleanup", {method:"POST",headers:{"content-type":"application/json"},body:JSON.stringify({scope:"build_cache",retention_hours:retention})});
      notify(
        result.available
          ? `构建缓存清理完成，释放 ${result.reclaimed ?? "0B"}`
          : `清理失败：${result.error ?? ""}`,
      );
      load();
    } catch (e) {
      notify(`清理失败：${String(e)}`);
    }
    setBusy(false);
  };
  const saveDockerPrefs = async (next: Prefs) => {
    setPrefs(next);
    try {
      await request("/api/settings", {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          docker_cleanup_after_run: next.docker_cleanup_after_run,
          docker_cleanup_on_delete: next.docker_cleanup_on_delete,
          docker_cache_retention_hours: next.docker_cache_retention_hours,
        }),
      });
    } catch (e) {
      notify(`Docker 设置保存失败：${String(e)}`);
    }
  };
  const activeRuns = storage?.active_runs ?? 0;
  return (
    <section className="panel">
      <h2>Docker 存储与清理</h2>
      {!storage ? (
        <p className="muted">正在读取 Docker 存储信息…</p>
      ) : !storage.available ? (
        <p className="muted">Docker 不可用：{storage.error}</p>
      ) : (
        <>
          <div className="metrics compact">
            <Metric
              label="镜像实际总占用"
              value={formatBytes(storage.images?.size_bytes)}
              hint={`${storage.images?.count ?? 0} 个条目（共享层不重复计算）`}
            />
            <Metric
              label="可回收镜像空间"
              value={formatBytes(storage.images?.reclaimable_bytes)}
            />
            <Metric
              label="本工具 Trial 镜像"
              value={`${storage.images?.managed_count ?? 0} 个`}
              hint={
                storage.images?.orphaned_count
                  ? `另有 ${storage.images.orphaned_count} 个孤儿条目`
                  : undefined
              }
            />
            <Metric
              label="构建缓存"
              value={formatBytes(storage.build_cache?.size_bytes)}
              hint={`保留期 ${Math.round(prefs.docker_cache_retention_hours / 24)} 天`}
            />
          </div>
          {scan && (
            <p className="muted">
              扫描结果：{scan.imageCount} 个可清理镜像条目，预计释放独占空间{" "}
              {scan.reclaimable ? formatBytes(scan.reclaimable) : "未知"}
            </p>
          )}
          <div className="actions">
            <label className="retentionselect">构建缓存保留
              <select value={prefs.docker_cache_retention_hours} onChange={(e)=>saveDockerPrefs({...prefs,docker_cache_retention_hours:+e.target.value})}>
                <option value={24}>1 天</option><option value={72}>3 天</option><option value={168}>7 天</option><option value={336}>14 天</option><option value={720}>30 天</option><option value={2160}>90 天</option>
              </select>
            </label>
            <button className="secondary" disabled={busy} onClick={load}>
              <RefreshCw />
              刷新
            </button>
            <button
              className="secondary"
              disabled={busy}
              onClick={scanResources}
            >
              扫描可清理资源
            </button>
            <button
              className="secondary"
              disabled={busy || activeRuns > 0}
              onClick={cleanImages}
            >
              <Trash2 />
              清理 Trial 镜像
            </button>
            <button
              className="secondary"
              disabled={busy || activeRuns > 0}
              onClick={() => cleanCache(false)}
            >
              <Trash2 />
              清理过期构建缓存
            </button>
            <button className="danger" disabled={busy || activeRuns > 0} onClick={() => cleanCache(true)}><Trash2 />清空全部构建缓存</button>
          </div>
          {activeRuns > 0 && (
            <p className="muted">
              有 {activeRuns} 个运行未结束，批量清理已禁用。
            </p>
          )}
          <div className="toggles">
            <label>
              <input
                type="checkbox"
                checked={prefs.docker_cleanup_after_run}
                onChange={(e) =>
                  saveDockerPrefs({
                    ...prefs,
                    docker_cleanup_after_run: e.target.checked,
                  })
                }
              />
              运行结束后自动清理该运行的 Trial 镜像
            </label>
            <label>
              <input
                type="checkbox"
                checked={prefs.docker_cleanup_on_delete}
                onChange={(e) =>
                  saveDockerPrefs({
                    ...prefs,
                    docker_cleanup_on_delete: e.target.checked,
                  })
                }
              />
              删除历史运行时执行 Docker 兜底清理
            </label>
          </div>
          <p className="muted">
            清理只针对本工具创建的 Trial 镜像条目；任务基础镜像（ECR /
            ubuntu）与近期构建缓存始终保留，下次运行无需重新下载。
          </p>
        </>
      )}
    </section>
  );
}
function Diagnostics({ checks }: { checks: Check[] }) {
  return (
    <section className="panel">
      <h2>环境预检</h2>
      <div className="checks">
        {checks.map((c) => (
          <div key={c.name}>
            <b className={c.status}></b>
            <span>{c.name}</span>
            <em>{c.message}</em>
          </div>
        ))}
      </div>
    </section>
  );
}
function Empty({ text }: { text: string }) {
  return <section className="panel empty">{text}</section>;
}
createRoot(document.getElementById("root")!).render(<App />);
