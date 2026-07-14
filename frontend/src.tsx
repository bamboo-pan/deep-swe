import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  BarChart3,
  Box,
  CheckCircle2,
  CheckSquare2,
  ChevronLeft,
  ChevronRight,
  ClipboardList,
  Database,
  Download,
  FileCode2,
  Gauge,
  Play,
  RefreshCw,
  RotateCcw,
  Save,
  Settings as SettingsIcon,
  ShieldCheck,
  Sparkles,
  Square,
  SquareX,
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
  model_efforts: Record<string, string[]>;
  model_defaults: Record<string, string>;
  efforts: string[];
  provider_catalog: {
    source: "provider" | "fallback";
    models_authoritative: boolean;
    error?: string | null;
  };
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
  failure_summary?: string;
  retry_of?: string | null;
  retrying?: boolean;
  replaced?: boolean;
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
  passed?: boolean | null;
  reward: number | null;
  reported_cost_usd: number | null;
  estimated_cost_usd: number | null;
  created_at: string;
  tasks: string[];
  attempts_per_task: number;
  concurrency: number;
  infrastructure_max_retries?: number;
  agent_max_steps?: number;
  codex_request_max_retries?: number;
  codex_stream_max_retries?: number;
  codex_stream_idle_timeout_seconds?: number;
  progress?: {
    completed?: number;
    total: number;
    passed: number;
    percent?: number;
  };
  task_progress?: {
    passed: number;
    total: number;
  };
  trials?: Trial[];
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
  agent_timeout_seconds: number;
  verifier_timeout_seconds: number;
  infrastructure_max_retries: number;
  agent_max_steps: number;
  docker_cleanup_after_run: boolean;
  docker_cleanup_on_delete: boolean;
  docker_cache_retention_hours: number;
  docker_cache_warning_gb: number;
  run_budget_usd: number;
  trial_budget_usd: number;
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
type CompareAnalysis = {
  model: string;
  reasoning_effort: string;
  analysis: string;
  summary: {
    total: number;
    consistent: number;
    better: number;
    worse: number;
    unavailable: number;
    strict: number;
    reference: number;
  };
  comparisons: Array<{
    task: string;
    run_id: number;
    run_code?: string;
    agent: string;
    model: string;
    reasoning_effort: string;
    comparison_scope: "strict" | "reference";
    verdict: "consistent" | "better" | "worse" | "unavailable";
    local: {
      pass_rate: number | null;
      attempts: number | null;
      avg_duration_seconds: number | null;
      avg_cost_usd: number | null;
      avg_steps: number | null;
    };
    official: {
      pass_rate: number | null;
      trials: number | null;
      avg_duration_seconds: number | null;
      avg_cost_usd: number | null;
      avg_steps: number | null;
    };
  }>;
};
// 生产构建由后端或反向代理同源服务；Vite 开发模式默认连接本地后端。
const apiBase = import.meta.env.VITE_API_BASE ||
  (import.meta.env.DEV ? "http://127.0.0.1:8765" : "");
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
const score = (value: number | null | undefined) =>
  value == null ? "—" : Number.isInteger(value) ? String(value) : value.toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
const statusLabel = (value: string) => value.replaceAll("_", " ").toUpperCase();
const failureTypeLabels: Record<string, string> = {
  AgentLimitExceeded: "执行步数已用完",
  CostLimitExceeded: "费用额度已用完",
  InfrastructureNetworkError: "基础设施网络错误",
  ResultMissing: "结果缺失",
  RunCancelled: "运行已取消",
  RunFailed: "运行失败",
  RunInterrupted: "运行被中断",
  UsageGuardTerminated: "用量护栏已终止",
  VerificationFailed: "验证未通过",
};
const failureTypeLabel = (value: string | undefined) =>
  value ? failureTypeLabels[value] || value : undefined;
const duration = (seconds: number | null | undefined) =>
  seconds == null
    ? "—"
    : seconds < 60
      ? `${Math.round(seconds)}s`
      : `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
const compactPercent = (value: number | null | undefined) => {
  if (value == null) return "—";
  const percentValue = value * 100;
  return `${percentValue.toFixed(Number.isInteger(percentValue) ? 0 : 1)}%`;
};
const compactMinutes = (seconds: number | null | undefined) =>
  seconds == null ? "—" : `${Math.max(Math.round(seconds / 60), 1)}m`;
const compactMoney = (value: number | null | undefined) =>
  value == null ? "—" : `$${value.toFixed(1)}`;
const compactNumber = (value: number | null | undefined) =>
  value == null ? "—" : value.toFixed(Number.isInteger(value) ? 0 : 1);
const terminal = new Set(["completed", "failed", "cancelled", "interrupted"]);
const parseCompareSelection = (key: string) => {
  const separator = key.indexOf(":");
  if (separator <= 0 || separator === key.length - 1) return null;
  const runId = Number(key.slice(0, separator));
  return Number.isInteger(runId)
    ? { runId, itemId: key.slice(separator + 1) }
    : null;
};
const pruneCompareSelections = (selected: string[], runs: Run[]) => {
  const itemsByRun = new Map(runs.map((run) => [
    run.id,
    new Set([...(run.trials || []).map((trial) => trial.id), ...run.tasks]),
  ]));
  const seen = new Set<string>();
  const next = selected.filter((key) => {
    const parsed = parseCompareSelection(key);
    if (!parsed || seen.has(key) || !itemsByRun.get(parsed.runId)?.has(parsed.itemId))
      return false;
    seen.add(key);
    return true;
  });
  return next.length === selected.length ? selected : next;
};

function Status({
  value,
  passed,
  reward,
}: {
  value: string;
  passed?: boolean | null;
  reward?: number | null;
}) {
  const failedCompletion =
    value === "completed" && (passed === false || (reward != null && reward < 1));
  const tone = failedCompletion
    ? "failure"
    : value === "completed"
      ? "success"
      : ["failed", "cancelled", "interrupted"].includes(value)
        ? "failure"
        : ["preflight", "running", "preparing_environment", "agent_running", "extracting_patch", "verifier"].includes(value)
          ? "active"
          : "waiting";
  return (
    <span className={`status ${value} ${tone}`}>{value.replaceAll("_", " ")}</span>
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
  const [navigationCollapsed, setNavigationCollapsed] = useState(
    () => localStorage.getItem("deepswe-navigation-collapsed") === "true",
  );
  const [selectedRun, setSelectedRun] = useState<number | null>(null);
  const [runStreamVersion, setRunStreamVersion] = useState(0);
  const [detail, setDetail] = useState<Run | null>(null);
  const [trialLog, setTrialLog] = useState("");
  const [activeTrial, setActiveTrial] = useState<Trial | null>(null);
  const [selectedResults, setSelectedResults] = useState<number[]>([]);
  const [compareIds, setCompareIds] = useState<string[]>([]);
  const [compareRuns, setCompareRuns] = useState<Run[]>([]);
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
  const [codexRequestMaxRetries, setCodexRequestMaxRetries] = useState("6");
  const [codexStreamMaxRetries, setCodexStreamMaxRetries] = useState("6");
  const [codexStreamIdleTimeout, setCodexStreamIdleTimeout] = useState("600");
  const [verification, setVerification] = useState(true);
  const [tier, setTier] = useState("standard");
  const refreshRuns = () =>
    request<Run[]>("/api/runs")
      .then((nextRuns) => {
        setRuns(nextRuns);
      })
      .catch(() => {});
  const refreshCompareRuns = () =>
    request<Run[]>("/api/compare/options")
      .then((nextRuns) => {
        setCompareRuns(nextRuns);
        setCompareIds((current) => pruneCompareSelections(current, nextRuns));
      })
      .catch(() => {});
  const refresh = () => {
    refreshRuns();
    if (tab === "compare") refreshCompareRuns();
    request<{ checks: Check[] }>("/api/diagnostics")
      .then((x) => setChecks(x.checks))
      .catch(() => {});
  };
  useEffect(() => {
    localStorage.setItem(
      "deepswe-navigation-collapsed",
      String(navigationCollapsed),
    );
  }, [navigationCollapsed]);
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
    if (!boot) return;
    const supported = boot.model_efforts[model] || boot.efforts;
    if (!supported.includes(effort)) {
      setEffort(boot.model_defaults[model] || supported[0]);
    }
  }, [boot, model, effort]);
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
        setDetail(terminal.has(next.status) && tab === "live" ? null : next);
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
  }, [selectedRun, tab, runStreamVersion]);
  useEffect(() => {
    if (tab === "compare") refreshCompareRuns();
    if (tab === "tasks" && !tasks.length)
      request<TaskInfo[]>("/api/tasks").then(setTasks);
    if (tab === "settings" && !prefs)
      request<Prefs>("/api/settings").then(setPrefs);
  }, [tab]);
  useEffect(() => {
    if (compareIds.length)
      request("/api/compare", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ items: compareIds }),
      }).then(setComparison);
    else setComparison(undefined);
  }, [compareIds]);
  const latest = runs[0];
  const attemptsNum = parseInt(attempts, 10);
  const trialCount =
    selectedTasks.length * (Number.isFinite(attemptsNum) ? attemptsNum : 0);
  const parallelAgentCount = allAgents ? boot?.agents.length || 0 : 1;
  const activeWorkersPerAgent = Math.min(concurrency, trialCount);
  const totalParallelTasks = activeWorkersPerAgent * parallelAgentCount;
  const parallelRisk =
    totalParallelTasks <= 12
      ? { className: "normal", label: "正常" }
      : totalParallelTasks <= 18
        ? { className: "warning", label: "资源峰值警告" }
        : { className: "danger", label: "需要高负载确认" };
  const selectModel = (nextModel: string) => {
    const supported = boot?.model_efforts[nextModel] || boot?.efforts || [];
    setModel(nextModel);
    setEffort((current) =>
      supported.includes(current)
        ? current
        : boot?.model_defaults[nextModel] || supported[0] || current,
    );
  };
  const start = async () => {
    if (starting) return;
    if (!selectedTasks.length) return setNotice("至少选择一个任务");
    const codexRequestMaxRetriesNum = parseInt(codexRequestMaxRetries, 10);
    const codexStreamMaxRetriesNum = parseInt(codexStreamMaxRetries, 10);
    const codexStreamIdleTimeoutNum = parseInt(codexStreamIdleTimeout, 10);
    if (!Number.isFinite(attemptsNum) || attemptsNum < 1 || attemptsNum > 10)
      return setNotice("每题次数需为 1-10 的整数");
    if (!Number.isFinite(codexRequestMaxRetriesNum) || codexRequestMaxRetriesNum < 0 || codexRequestMaxRetriesNum > 10)
      return setNotice("Codex HTTP 重试次数需为 0-10 的整数");
    if (!Number.isFinite(codexStreamMaxRetriesNum) || codexStreamMaxRetriesNum < 0 || codexStreamMaxRetriesNum > 10)
      return setNotice("Codex 流重试次数需为 0-10 的整数");
    if (!Number.isFinite(codexStreamIdleTimeoutNum) || codexStreamIdleTimeoutNum < 30 || codexStreamIdleTimeoutNum > 1800)
      return setNotice("Codex 流空闲超时需为 30-1800 秒");
    if (!Number.isInteger(concurrency) || concurrency < 1 || concurrency > 72)
      return setNotice("每 Agent 并发数需为 1-72 的整数");
    const agents = allAgents ? boot!.agents : [agent];
    const parallelTasks =
      Math.min(concurrency, selectedTasks.length * attemptsNum) * agents.length;
    if (parallelTasks > 72)
      return setNotice("总并行 Trial 数不能超过 72");
    if (
      parallelTasks >= 19 &&
      !confirm(
        `将同时运行最多 ${parallelTasks} 个 Trial。模型调用阶段通常资源占用较低，但集中构建和验证可能造成 CPU、磁盘与内存峰值。\n确认继续？`,
      )
    )
      return;
    setStarting(true);
    setNotice("正在创建运行…");
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
            parallel_agent_count: agents.length,
            confirm_high_concurrency: parallelTasks >= 19,
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
          ? `已并行创建 ${created.length} 个 Agent 运行（总并行 Trial 上限 ${created.length * Math.min(concurrency, selectedTasks.length * attemptsNum)}）`
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
  const savePrefs = async () => {
    if (!prefs) return;
    if (!Number.isInteger(prefs.agent_timeout_seconds) || prefs.agent_timeout_seconds < 60 || prefs.agent_timeout_seconds > 21600)
      return setNotice("Agent 超时需为 60-21600 秒");
    if (!Number.isInteger(prefs.verifier_timeout_seconds) || prefs.verifier_timeout_seconds < 60 || prefs.verifier_timeout_seconds > 7200)
      return setNotice("Verifier 超时需为 60-7200 秒");
    if (!Number.isInteger(prefs.infrastructure_max_retries) || prefs.infrastructure_max_retries < 0 || prefs.infrastructure_max_retries > 6)
      return setNotice("基础设施重试次数需为 0-6 的整数");
    if (!Number.isInteger(prefs.agent_max_steps) || prefs.agent_max_steps < 10 || prefs.agent_max_steps > 500)
      return setNotice("最大步数需为 10-500 的整数");
    const saved = await request<Prefs>("/api/settings", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify(prefs),
    });
    const nextBoot = await request<Boot>("/api/bootstrap");
    setPrefs(saved);
    setBoot(nextBoot);
    if (!nextBoot.models.includes(model)) {
      setModel(nextBoot.defaults.model);
      setEffort(nextBoot.defaults.reasoning_effort);
    }
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
    const deletedIds = new Set(ids.filter((id) => !failedIds.includes(id)));
    setSelectedResults(failedIds);
    if (deletedIds.size) {
      setCompareIds((current) => {
        const next = current.filter((key) => {
          const parsed = parseCompareSelection(key);
          return parsed && !deletedIds.has(parsed.runId);
        });
        return next.length === current.length ? current : next;
      });
    }
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
  const deleteTrials = async (trialIds: string[]) => {
    if (!detail || !trialIds.length) return trialIds;
    const runId = detail.id;
    if (
      !confirm(
        `删除选中的 ${trialIds.length} 条 Trial 记录、日志和结果文件？\n此操作不会删除整个 Run。`,
      )
    )
      return trialIds;
    const failures: { id: string; message: string }[] = [];
    let removedImages = 0;
    for (const trialId of trialIds) {
      try {
        const body = await request<{
          docker_cleanup?: { removed_images?: string[] };
        }>(
          `/api/runs/${runId}/trials/${encodeURIComponent(trialId)}`,
          { method: "DELETE" },
        );
        removedImages += body.docker_cleanup?.removed_images?.length ?? 0;
      } catch (e) {
        failures.push({ id: trialId, message: String(e) });
      }
    }
    const failedIds = failures.map((item) => item.id);
    const deletedIds = new Set(trialIds.filter((id) => !failedIds.includes(id)));
    setCompareIds((current) => current.filter((key) => {
      const parsed = parseCompareSelection(key);
      return !(parsed?.runId === runId && deletedIds.has(parsed.itemId));
    }));
    if (activeTrial && deletedIds.has(activeTrial.id)) {
      setActiveTrial(null);
      setTrialLog("");
    }
    const [nextDetail, nextRuns] = await Promise.all([
      request<Run>(`/api/runs/${runId}`),
      request<Run[]>("/api/runs"),
    ]);
    setDetail(nextDetail);
    setRuns(nextRuns);
    refreshCompareRuns();
    setNotice(
      failures.length
        ? `${trialIds.length - failures.length} 条 Trial 已删除，${failures.length} 条失败：${failures[0].message}`
        : `已删除 ${trialIds.length} 条 Trial 记录${removedImages ? `，清理 ${removedImages} 个 Trial 镜像` : ""}`,
    );
    return failedIds;
  };
  const retryTrials = async (trialIds: string[]) => {
    if (!detail || !trialIds.length) return false;
    const runId = detail.id;
    if (
      !confirm(
        `重试选中的 ${trialIds.length} 条 Trial？\n旧结果会删除并在原 Trial 位置替换；任务、Agent 和模型沿用原 Run，运行时限制使用当前 Settings。`,
      )
    )
      return false;
    let result: { retry_count: number };
    try {
      result = await request<{ retry_count: number }>(
        `/api/runs/${runId}/trials/retry`,
        {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({ trial_ids: trialIds }),
        },
      );
    } catch (error) {
      setNotice(`Trial 重试提交失败：${String(error)}`);
      return false;
    }
    setRunStreamVersion((version) => version + 1);
    const [nextDetail, nextRuns] = await Promise.allSettled([
      request<Run>(`/api/runs/${runId}`),
      request<Run[]>("/api/runs"),
    ]);
    if (nextDetail.status === "fulfilled") setDetail(nextDetail.value);
    if (nextRuns.status === "fulfilled") setRuns(nextRuns.value);
    setActiveTrial(null);
    setTrialLog("");
    refreshCompareRuns();
    setNotice(`已提交 ${result.retry_count} 条 Trial 重试，旧结果将原位替换`);
    return true;
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
    <div className={`shell ${navigationCollapsed ? "navigationCollapsed" : ""}`}>
      <aside className="appSidebar">
        <button
          className="sidebarToggle"
          onClick={() => setNavigationCollapsed((collapsed) => !collapsed)}
          aria-label={navigationCollapsed ? "展开主导航" : "收起主导航"}
          aria-expanded={!navigationCollapsed}
          aria-controls="primary-navigation"
          title={navigationCollapsed ? "展开主导航" : "收起主导航"}
        >
          {navigationCollapsed ? <ChevronRight /> : <ChevronLeft />}
        </button>
        <div className="brand">
          <Box />{" "}
          <span>
            DeepSWE<small>REGRESSION LAB</small>
          </span>
        </div>
        <nav id="primary-navigation">
          {nav.map(([name, Icon]) => (
            <button
              key={name}
              className={tab === name ? "active" : ""}
              onClick={() => setTab(name)}
              title={navigationCollapsed ? name : undefined}
            >
              <Icon />
              <span>{name}</span>
            </button>
          ))}
        </nav>
        <div className="local">
          <ShieldCheck />
          <span>
            仅本机访问
            <small>127.0.0.1:8765</small>
          </span>
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
                三 Agent 同跑会创建三条可独立查看和取消的运行。
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
              总并行 Trial 上限 {totalParallelTasks}
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
                {trialCount} TRIALS
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
                  onChange={(e) => selectModel(e.target.value)}
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
                  {(boot.model_efforts[model] || boot.efforts).map((x) => (
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
                每 Agent 并发数
                <input
                  type="number"
                  min="1"
                  max="72"
                  step="1"
                  value={concurrency}
                  onChange={(e) => setConcurrency(+e.target.value)}
                />
                <small className={`risk ${parallelRisk.className}`}>
                  总并行 {totalParallelTasks} · {parallelRisk.label}
                </small>
              </label>
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
            selected={selectedResults}
            setSelected={setSelectedResults}
            remove={deleteResults}
            removeTrials={deleteTrials}
            retryTrials={retryTrials}
          />
        )}
        {tab === "compare" && (
          <Compare
            runs={compareRuns}
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
              <Status value={r.status} passed={r.passed} />
              {r.agent}
              <small>{r.run_code || `RUN-${String(r.id).padStart(6, "0")}`}</small>
            </button>
          ))}
        </section>
      )}
      <section className="panel">
        <div className="panelhead">
          <div>
            <Status value={detail.status} passed={detail.passed} />
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
            <Status value={t.status} reward={t.reward} />
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
  selected,
  setSelected,
  remove,
  removeTrials,
  retryTrials,
}: {
  runs: Run[];
  detail: Run | null;
  openRun: (id: number) => void;
  openTrial: (t: Trial) => void;
  activeTrial: Trial | null;
  trialLog: string;
  selected: number[];
  setSelected: (ids: number[]) => void;
  remove: () => void;
  removeTrials: (ids: string[]) => Promise<string[]>;
  retryTrials: (ids: string[]) => Promise<boolean>;
}) {
  const all = runs.length > 0 && selected.length === runs.length;
  const [runListCollapsed, setRunListCollapsed] = useState(
    () => localStorage.getItem("deepswe-run-list-collapsed") === "true",
  );
  const [selectedTrials, setSelectedTrials] = useState<string[]>([]);
  const [deletingTrials, setDeletingTrials] = useState(false);
  const [submittingRetry, setSubmittingRetry] = useState(false);
  const [trialStatusFilter, setTrialStatusFilter] = useState("all");
  const trials = detail?.trials || [];
  const trialIds = trials.map((trial) => trial.id);
  const trialStatuses = [...new Set(trials.map((trial) => trial.status))].sort();
  const visibleTrials = trials.filter(
    (trial) => trialStatusFilter === "all" || trial.status === trialStatusFilter,
  );
  const visibleTrialIds = visibleTrials.map((trial) => trial.id);
  const allVisibleTrialsSelected = visibleTrialIds.length > 0 &&
    visibleTrialIds.every((id) => selectedTrials.includes(id));
  const canManageTrials = Boolean(detail && terminal.has(detail.status));
  useEffect(() => {
    setSelectedTrials([]);
    setTrialStatusFilter("all");
  }, [detail?.id]);
  useEffect(() => {
    setSelectedTrials((current) => current.filter((id) => trialIds.includes(id)));
  }, [detail?.trials]);
  useEffect(() => {
    localStorage.setItem(
      "deepswe-run-list-collapsed",
      String(runListCollapsed),
    );
  }, [runListCollapsed]);
  const deleteSelectedTrials = async () => {
    if (!selectedTrials.length || deletingTrials) return;
    setDeletingTrials(true);
    try {
      setSelectedTrials(await removeTrials(selectedTrials));
    } finally {
      setDeletingTrials(false);
    }
  };
  const retrySelectedTrials = async () => {
    if (!selectedTrials.length || submittingRetry) return;
    setSubmittingRetry(true);
    try {
      if (await retryTrials(selectedTrials)) setSelectedTrials([]);
    } finally {
      setSubmittingRetry(false);
    }
  };
  return (
    <div className={`resultsLayout ${runListCollapsed ? "runlistCollapsed" : ""}`}>
      <section className={`panel runlist ${runListCollapsed ? "isCollapsed" : ""}`}>
        {runListCollapsed ? (
          <button
            className="paneToggle runlistExpand"
            onClick={() => setRunListCollapsed(false)}
            aria-label="展开 Job 历史"
            aria-expanded={false}
            title="展开 Job 历史"
          >
            <ChevronRight />
          </button>
        ) : (
          <>
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
                className="paneToggle"
                onClick={() => setRunListCollapsed(true)}
                aria-label="收起 Job 历史"
                aria-expanded={true}
                title="收起 Job 历史"
              >
                <ChevronLeft />
              </button>
              <button
                className="danger"
                disabled={!selected.length}
                onClick={remove}
              >
                <Trash2 />
                删除 {selected.length || ""}
              </button>
            </div>
            <div className="runlistContent">
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
                      <Status value={r.status} passed={r.passed} />
                      <b>{r.agent}</b>
                      <span>
                        {r.run_code || `RUN-${String(r.id).padStart(6, "0")}`}
                        <small>{r.job_name}</small>
                      </span>
                      <em className={r.status === "completed" ? (r.passed ? "success" : "failure") : "waiting"}>
                        {r.progress ? `${r.progress.passed}/${r.progress.total}` : "—"}
                      </em>
                    </button>
                  </div>
                ))
              ) : (
                <Empty text="暂无运行" />
              )}
            </div>
          </>
        )}
      </section>
      <div>
        {detail ? (
          <>
            <section className="metrics">
              <Metric
                label="通过 Trial"
                value={`${detail.progress?.passed || 0}/${detail.progress?.total || detail.tasks.length * detail.attempts_per_task}`}
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
            <section className="panel">
              <div className="panelhead">
                <h2>Trial 结果</h2>
                <div className="trialresulttools">
                  <select
                    className="trialstatusfilter"
                    value={trialStatusFilter}
                    onChange={(event) => setTrialStatusFilter(event.target.value)}
                    disabled={!trials.length}
                    aria-label="按 Trial 状态筛选"
                    title="按 Trial 状态筛选"
                  >
                    <option value="all">全部状态</option>
                    {trialStatuses.map((status) => (
                      <option key={status} value={status}>{statusLabel(status)}</option>
                    ))}
                  </select>
                  <label title={canManageTrials ? "选择当前状态筛选下的全部 Trial" : "运行结束后才能管理 Trial"}>
                    <input
                      type="checkbox"
                      checked={allVisibleTrialsSelected}
                      disabled={!canManageTrials || !visibleTrialIds.length}
                      onChange={() => setSelectedTrials((current) =>
                        allVisibleTrialsSelected
                          ? current.filter((id) => !visibleTrialIds.includes(id))
                          : [...new Set([...current, ...visibleTrialIds])],
                      )}
                    />
                    全选
                  </label>
                  <span>已选 {selectedTrials.length}</span>
                  <button
                    className="secondary"
                    disabled={!canManageTrials || !selectedTrials.length || submittingRetry || deletingTrials}
                    onClick={retrySelectedTrials}
                    title="沿用原 Run 的任务、Agent 和模型，使用最新 Settings 原位替换所选 Trial"
                  >
                    <RotateCcw />
                    {submittingRetry ? "提交中…" : "重试"}
                  </button>
                  <button
                    className="danger"
                    disabled={!canManageTrials || !selectedTrials.length || deletingTrials || submittingRetry}
                    onClick={deleteSelectedTrials}
                    title="删除选中的 Trial 记录、日志和结果文件"
                  >
                    <Trash2 />
                    {deletingTrials ? "删除中…" : "删除"}
                  </button>
                </div>
              </div>
              <div className="table">
                <div className="tr">
                  <span />
                  <b>Task</b>
                  <b>Status</b>
                  <b>失败原因</b>
                  <b>Reward</b>
                  <b>F2P / P2P</b>
                  <b>Duration</b>
                  <b>Tokens (I / C / O)</b>
                  <b>Cost</b>
                  <b>Steps</b>
                </div>
                {visibleTrials.map((t) => (
                  <div
                    className="tr trialrow"
                    key={t.id}
                    onClick={() => openTrial(t)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter" || event.key === " ") openTrial(t);
                    }}
                    role="button"
                    tabIndex={0}
                  >
                    <input
                      type="checkbox"
                      checked={selectedTrials.includes(t.id)}
                      disabled={!canManageTrials}
                      aria-label={`选择 ${t.trial_code || t.id}`}
                      onClick={(event) => event.stopPropagation()}
                      onChange={(event) => setSelectedTrials(
                        event.target.checked
                          ? [...selectedTrials, t.id]
                          : selectedTrials.filter((id) => id !== t.id),
                      )}
                    />
                    <span className="tabletask">
                      <b>{t.task_code}</b>
                      <span>{t.task_title || t.task}</span>
                      <small>{t.task_slug || t.task}</small>
                    </span>
                    <Status value={t.status} reward={t.reward} />
                    <span
                      className={`failurecell ${t.failure_type || t.failure_message ? "hasfailure" : ""}`}
                      title={[failureTypeLabel(t.failure_type), t.failure_message].filter(Boolean).join("：")}
                    >
                      {t.failure_type && <b>{failureTypeLabel(t.failure_type)}</b>}
                      <span>{t.failure_message || "—"}</span>
                    </span>
                    <span>{score(t.reward)}</span>
                    <span>
                      {score(t.f2p)} / {score(t.p2p)}
                    </span>
                    <span>{duration(t.duration_seconds)}</span>
                    <span className="trialtokens">
                      <span><i>I</i>{number(t.input_tokens)}</span>
                      <span><i>C</i>{number(t.cached_tokens)}</span>
                      <span><i>O</i>{number(t.output_tokens)}</span>
                    </span>
                    <span>{money(t.reported_cost_usd)}</span>
                    <span>{t.steps ?? "—"}</span>
                  </div>
                ))}
                {!visibleTrials.length && (
                  <div className="trialfilterempty">
                    {trials.length ? "没有符合当前状态筛选的 Trial。" : "当前 Run 暂无 Trial 结果。"}
                  </div>
                )}
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
        <Metric label="Partial" value={score(trial.partial)} />
        <Metric label="Input / Cache" value={`${number(trial.input_tokens)} / ${number(trial.cached_tokens)}`} />
        <Metric label="Output" value={number(trial.output_tokens)} />
        <Metric label="Cost" value={money(trial.reported_cost_usd)} />
        <Metric label="Patch" value={`${number(trial.patch_bytes)} B`} />
      </div>
      {(trial.failure_type || trial.failure_message) && (
        <div className="trialfailure">
          <h3>失败原因</h3>
          <pre className="failure">
            {[failureTypeLabel(trial.failure_type), trial.failure_message].filter(Boolean).join("：")}
          </pre>
        </div>
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
type ComparisonTone = "better" | "equal" | "worse" | "unknown";
const modelKey = (value: string | null | undefined) =>
  (value || "").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
const comparisonTone = (
  value: number | null | undefined,
  baseline: number | null | undefined,
  higherIsBetter: boolean,
): ComparisonTone => {
  if (value == null || baseline == null) return "unknown";
  const tolerance = higherIsBetter ? 0.0005 : Math.max(Math.abs(baseline) * 0.005, 0.01);
  if (Math.abs(value - baseline) <= tolerance) return "equal";
  return (higherIsBetter ? value > baseline : value < baseline) ? "better" : "worse";
};
const comparisonLabel = (tone: ComparisonTone) => ({
  better: "优于基准",
  equal: "持平",
  worse: "差于基准",
  unknown: "无精确基准",
}[tone]);
const analysisVerdictLabel = (verdict: string) => ({
  consistent: "一致",
  better: "变好",
  worse: "变坏",
  unavailable: "无法判断",
}[verdict] || verdict);

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
  const [statusFilter, setStatusFilter] = useState("all");
  const [analysis, setAnalysis] = useState<CompareAnalysis>();
  const [analysisError, setAnalysisError] = useState("");
  const [analyzing, setAnalyzing] = useState(false);
  const analysisRef = useRef<HTMLElement>(null);
  const taskNames = [...new Set(runs.flatMap((run) => (run.trials || []).map((trial) => trial.task)))].sort();
  const trialStatuses = [...new Set(runs.flatMap((run) =>
    (run.trials || []).map((trial) => trial.status),
  ))].sort();
  const matchesRunFilters = (run: Run) =>
    (agentFilter === "all" || run.agent === agentFilter) &&
    (modelFilter === "all" || run.model === modelFilter) &&
    (effortFilter === "all" || run.reasoning_effort === effortFilter);
  const matchesTrialFilters = (trial: Trial) =>
    statusFilter === "all" || trial.status === statusFilter;
  const filteredRuns = runs.filter(matchesRunFilters);
  const groupedTrials = taskNames
    .filter((task) => taskFilter === "all" || task === taskFilter)
    .map((task) => ({
      task,
      items: filteredRuns.flatMap((run) =>
        (run.trials || [])
          .filter((trial) => trial.task === task && matchesTrialFilters(trial))
          .map((trial) => ({ run, trial })),
      ),
    }))
    .filter((group) => group.items.length);
  const visibleSelectionKeys = groupedTrials.flatMap((group) =>
    group.items.map(({ run, trial }) => `${run.id}:${trial.id}`),
  );
  const selectedSet = new Set(selected);
  const allVisibleSelected = visibleSelectionKeys.length > 0 &&
    visibleSelectionKeys.every((key) => selectedSet.has(key));
  const runById = new Map(runs.map((run) => [run.id, run]));
  const selectedEntries = selected.flatMap((key) => {
    const parsed = parseCompareSelection(key);
    const run = parsed ? runById.get(parsed.runId) : undefined;
    const trial = run?.trials?.find((item) => item.id === parsed?.itemId);
    return run && trial ? [{ key, run, trial }] : [];
  });
  const visibleSelectedEntries = selectedEntries.filter(({ run, trial }) =>
    matchesRunFilters(run) &&
    matchesTrialFilters(trial) &&
    (taskFilter === "all" || trial.task === taskFilter),
  );
  const analysisSelectionKeys = visibleSelectedEntries.map(({ key }) => key);
  const analysisScopeKey = [...analysisSelectionKeys].sort().join("|");
  useEffect(() => {
    setAnalysis(undefined);
    setAnalysisError("");
  }, [analysisScopeKey]);
  useEffect(() => {
    if (!analysis && !analysisError) return;
    const frame = requestAnimationFrame(() => {
      analysisRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
    return () => cancelAnimationFrame(frame);
  }, [analysis, analysisError]);
  const analyzeSelection = async () => {
    if (!analysisSelectionKeys.length || analyzing) return;
    setAnalyzing(true);
    setAnalysisError("");
    try {
      setAnalysis(await request<CompareAnalysis>("/api/compare/analyze", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ items: analysisSelectionKeys }),
      }));
    } catch (error) {
      setAnalysis(undefined);
      setAnalysisError(error instanceof Error ? error.message : String(error));
    } finally {
      setAnalyzing(false);
    }
  };
  const visibleRunIds = new Set(visibleSelectedEntries.map(({ run }) => run.id));
  const visibleTaskNames = new Set(visibleSelectedEntries.map(({ trial }) => trial.task));
  const visibleComparisonRuns = (comparison?.runs || []).filter((run: Run) =>
    visibleRunIds.has(run.id),
  );
  const visibleTasks = (comparison?.tasks || []).filter((row: any) =>
    visibleTaskNames.has(row.task),
  );
  const runHasSelectedTask = (runId: number, task: string) =>
    visibleSelectedEntries.some(({ run, trial }) => run.id === runId && trial.task === task);
  const exactBaseline = (row: any, run: Run) =>
    (row.official_configurations || []).find((item: any) =>
      modelKey(item.model) === modelKey(run.model) &&
      (item.reasoning_effort || "none").toLowerCase() === (run.reasoning_effort || "none").toLowerCase(),
    );
  const visibleOfficialConfigurations = (row: any) =>
    (row.official_configurations || []).filter((item: any) =>
      visibleComparisonRuns.some((run: Run) =>
        runHasSelectedTask(run.id, row.task) &&
        modelKey(item.model) === modelKey(run.model) &&
        (item.reasoning_effort || "none").toLowerCase() === (run.reasoning_effort || "none").toLowerCase(),
      ),
    );
  const summaries = visibleComparisonRuns.map((run: Run) => {
    const selectedTrials = visibleSelectedEntries
      .filter((entry) => entry.run.id === run.id)
      .map((entry) => entry.trial);
    const values = visibleTasks
      .filter((row: any) => runHasSelectedTask(run.id, row.task))
      .map((row: any) => row.runs[String(run.id)] || {});
    const total = (field: string) => {
      const present = values.map((value: any) => value[field]).filter((value: any) => value != null);
      return present.length ? present.reduce((sum: number, value: number) => sum + value, 0) : null;
    };
    return {
      run,
      passed: selectedTrials.filter((trial) => trial.reward === 1).length,
      total: selectedTrials.length,
      cost: total("total_estimated_cost_usd"),
      input: total("total_input_tokens"),
    };
  });
  return (
    <>
      <section className="panel">
        <div className="panelhead comparepickerhead">
          <div>
            <h2>选择结果</h2>
          </div>
          <div className="compareselectiontools">
            <b>已选 {selected.length}</b>
            <small className="compareanalysismodel">分析模型 gpt-5.6-sol · max</small>
            <button
              className="secondary compareanalysisbutton"
              disabled={!analysisSelectionKeys.length || analyzing}
              onClick={analyzeSelection}
              title={analysisSelectionKeys.length ? "分析当前筛选中已选择的 Trial" : "当前筛选中没有已选择的 Trial"}
            >
              <Sparkles />
              {analyzing ? "分析中" : "AI 分析"}
            </button>
            <button
              className="secondary"
              disabled={!visibleSelectionKeys.length || allVisibleSelected}
              onClick={() => setSelected([...new Set([...selected, ...visibleSelectionKeys])])}
              title="选择当前筛选条件下的全部 Trial 结果"
            >
              <CheckSquare2 />
              全选当前筛选
            </button>
            <button
              className="secondary"
              disabled={!selected.length}
              onClick={() => setSelected([])}
              title="清空全部对比选择"
            >
              <SquareX />
              清空选择
            </button>
          </div>
        </div>
        <div className="comparefilters">
          <label>Task<select value={taskFilter} onChange={(e)=>setTaskFilter(e.target.value)}><option value="all">全部任务</option>{taskNames.map(x=><option key={x} value={x}>{x}</option>)}</select></label>
          <label>Agent<select value={agentFilter} onChange={(e)=>setAgentFilter(e.target.value)}><option value="all">全部 Agent</option>{[...new Set(runs.map(x=>x.agent))].map(x=><option key={x}>{x}</option>)}</select></label>
          <label>模型<select value={modelFilter} onChange={(e)=>setModelFilter(e.target.value)}><option value="all">全部模型</option>{[...new Set(runs.map(x=>x.model))].map(x=><option key={x}>{x}</option>)}</select></label>
          <label>思考强度<select value={effortFilter} onChange={(e)=>setEffortFilter(e.target.value)}><option value="all">全部强度</option>{[...new Set(runs.map(x=>x.reasoning_effort))].map(x=><option key={x}>{x}</option>)}</select></label>
          <label>状态<select value={statusFilter} onChange={(e)=>setStatusFilter(e.target.value)}><option value="all">全部状态</option>{trialStatuses.map(status=><option key={status} value={status}>{statusLabel(status)}</option>)}</select></label>
        </div>
        {groupedTrials.map(group=><div className="comparegroup" key={group.task}><h3>{group.task}</h3><div className="comparepick">
          {group.items.map(({ run, trial }) => { const selectionKey = `${run.id}:${trial.id}`; return (
            <label key={selectionKey}>
              <input
                type="checkbox"
                checked={selectedSet.has(selectionKey)}
                onChange={(e) =>
                  setSelected(
                    e.target.checked
                      ? [...selected, selectionKey]
                      : selected.filter((x) => x !== selectionKey),
                  )
                }
              />
              <Status value={trial.status} reward={trial.reward} />
              <span>
                {run.agent} · {run.model} · {run.reasoning_effort}
              </span>
              <em>
                <b>{trial.trial_code || `${run.run_code || `RUN-${String(run.id).padStart(6, "0")}`} / A${trial.attempt || 1}`}</b>
                <small>reward {score(trial.reward)} · {duration(trial.duration_seconds)}</small>
              </em>
            </label>
          )})}
        </div></div>)}
        {!groupedTrials.length && (
          <div className="comparefilterempty">没有符合当前筛选条件的 Trial 结果。</div>
        )}
      </section>
      {(analysis || analysisError) && (
        <section className="panel compareanalysis" ref={analysisRef}>
          <div className="comparetitle">
            <h2>AI 对比分析</h2>
            {analysis && <span className="compareanalysismeta">分析模型 {analysis.model} · {analysis.reasoning_effort}</span>}
          </div>
          {analysisError ? (
            <div className="compareanalysiserror">{analysisError}</div>
          ) : analysis && (
            <>
              <div className="compareanalysissummary">
                <span>共 {analysis.summary.total}</span>
                <span className="equal">一致 {analysis.summary.consistent}</span>
                <span className="better">变好 {analysis.summary.better}</span>
                <span className="worse">变坏 {analysis.summary.worse}</span>
                <span>无法判断 {analysis.summary.unavailable}</span>
                <span>严格 {analysis.summary.strict} · 参考 {analysis.summary.reference}</span>
              </div>
              <div className="compareanalysistext">{analysis.analysis}</div>
              <div className="compareanalysisfacts">
                <b>Task / Run</b><b>结论</b><b>通过率</b><b>平均用时</b><b>平均成本</b><b>平均步骤</b>
                {analysis.comparisons.map((item) => {
                  const verdictTone = item.verdict === "consistent"
                    ? "equal"
                    : item.verdict === "unavailable" ? "unknown" : item.verdict;
                  const durationTone = comparisonTone(item.local.avg_duration_seconds, item.official.avg_duration_seconds, false);
                  const costTone = comparisonTone(item.local.avg_cost_usd, item.official.avg_cost_usd, false);
                  const stepsTone = comparisonTone(item.local.avg_steps, item.official.avg_steps, false);
                  return (
                    <React.Fragment key={`${item.run_id}:${item.task}`}>
                      <span><b>{item.task}</b><small>{item.run_code || `RUN-${String(item.run_id).padStart(6, "0")}`} · {item.agent} · {item.model} · {item.reasoning_effort}</small></span>
                      <span className={`comparevalue ${verdictTone}`}>{analysisVerdictLabel(item.verdict)}</span>
                      <span className={`compareanalysismetric ${verdictTone}`}><b>{compactPercent(item.local.pass_rate)}</b><small>官方 {compactPercent(item.official.pass_rate)} · {item.local.attempts ?? 0}/{item.official.trials ?? 0} trials</small></span>
                      <span className={`compareanalysismetric ${durationTone}`}><b>{compactMinutes(item.local.avg_duration_seconds)}</b><small>官方 {compactMinutes(item.official.avg_duration_seconds)}</small></span>
                      <span className={`compareanalysismetric ${costTone}`}><b>{compactMoney(item.local.avg_cost_usd)}</b><small>官方 {compactMoney(item.official.avg_cost_usd)}</small></span>
                      <span className={`compareanalysismetric ${stepsTone}`}><b>{compactNumber(item.local.avg_steps)}</b><small>官方 {compactNumber(item.official.avg_steps)}</small></span>
                    </React.Fragment>
                  );
                })}
              </div>
            </>
          )}
        </section>
      )}
      {summaries.length > 0 && (
        <>
          <section className="metrics">
            {summaries.map(({ run, passed, total, cost, input }: any) => (
              <Metric
                key={run.id}
                label={`${run.run_code || `RUN-${String(run.id).padStart(6, "0")}`} ${run.agent}`}
                value={`${passed}/${total}`}
                hint={`${money(cost)} · ${number(input)} input`}
              />
            ))}
          </section>
          <section className="panel">
            <div className="comparetitle">
              <h2>同任务详细对比</h2>
              <div className="comparisonlegend"><i className="better"></i>优于 <i className="equal"></i>持平 <i className="worse"></i>差于</div>
            </div>
            {visibleTasks.map((row: any) => {
              const localRuns = visibleComparisonRuns.filter((run: Run) =>
                runHasSelectedTask(run.id, row.task) && Object.hasOwn(row.runs, String(run.id)),
              );
              const configurations = visibleOfficialConfigurations(row);
              return (
                <div className="comparetask" key={row.task}>
                  <h3>{row.task_code} · {row.task_title}</h3>
                  <div className="comparetable">
                    <b>运行 / 官方基准</b><b>通过率</b><b>平均耗时</b><b>Input / Cache / Output（每 Trial）</b><b>平均成本</b><b>平均步骤</b>
                    <span className="officialcell"><b>官方全部模型汇总</b><small>{row.official?.trials ? `${row.official.trials} trials` : "暂无数据"}</small></span>
                    <span className="officialcell">{percent(row.official?.pass_rate)}</span>
                    <span className="officialcell">{duration(row.official?.avg_duration_seconds)}</span>
                    <span className="officialcell comparetokens">{number(row.official?.avg_input_tokens)} / {number(row.official?.avg_cache_tokens)} / {number(row.official?.avg_output_tokens)}</span>
                    <span className="officialcell">{money(row.official?.avg_cost_usd)}</span>
                    <span className="officialcell">{row.official?.avg_steps ?? "—"}</span>
                    {configurations.map((baseline: any) => (
                      <React.Fragment key={`${baseline.model}:${baseline.reasoning_effort}`}>
                        <span className="officialcell exact"><b>官方精确基准</b><small>{baseline.model} · {baseline.reasoning_effort}{baseline.trials ? ` · ${baseline.trials} trials` : " · 暂无数据"}</small></span>
                        <span className="officialcell exact">{percent(baseline.pass_rate)}</span>
                        <span className="officialcell exact">{duration(baseline.avg_duration_seconds)}</span>
                        <span className="officialcell exact comparetokens">{number(baseline.avg_input_tokens)} / {number(baseline.avg_cache_tokens)} / {number(baseline.avg_output_tokens)}</span>
                        <span className="officialcell exact">{money(baseline.avg_cost_usd)}</span>
                        <span className="officialcell exact">{baseline.avg_steps ?? "—"}</span>
                      </React.Fragment>
                    ))}
                    {localRuns.map((run: Run) => {
                      const value = row.runs[String(run.id)] || {};
                      const baseline = exactBaseline(row, run);
                      const passTone = comparisonTone(value.pass_rate, baseline?.pass_rate, true);
                      return (
                        <React.Fragment key={run.id}>
                          <span className="localcell"><b>{run.agent}</b><small>{run.model} · {run.reasoning_effort}</small><em className={`comparisonbadge ${passTone}`}>{comparisonLabel(passTone)}</em></span>
                          <span className={`localcell comparevalue ${passTone}`}>{percent(value.pass_rate)}</span>
                          <span className={`localcell comparevalue ${comparisonTone(value.duration_seconds, baseline?.avg_duration_seconds, false)}`}>{duration(value.duration_seconds)}</span>
                          <span className="localcell comparetokens">
                            <i className={`comparevalue ${comparisonTone(value.input_tokens, baseline?.avg_input_tokens, false)}`}>{number(value.input_tokens)}</i> /
                            <i className={`comparevalue ${comparisonTone(value.cached_tokens, baseline?.avg_cache_tokens, false)}`}>{number(value.cached_tokens)}</i> /
                            <i className={`comparevalue ${comparisonTone(value.output_tokens, baseline?.avg_output_tokens, false)}`}>{number(value.output_tokens)}</i>
                          </span>
                          <span className={`localcell comparevalue ${comparisonTone(value.cost_usd, baseline?.avg_cost_usd, false)}`}>{money(value.cost_usd)}</span>
                          <span className={`localcell comparevalue ${comparisonTone(value.steps, baseline?.avg_steps, false)}`}>{value.steps ?? "—"}</span>
                        </React.Fragment>
                      );
                    })}
                  </div>
                </div>
              );
            })}
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
  const defaultEfforts =
    boot.model_efforts[prefs.default_model] || boot.efforts;
  const selectDefaultModel = (nextModel: string) => {
    const supported = boot.model_efforts[nextModel] || boot.efforts;
    setPrefs({
      ...prefs,
      default_model: nextModel,
      default_effort: supported.includes(prefs.default_effort)
        ? prefs.default_effort
        : boot.model_defaults[nextModel] || supported[0],
    });
  };
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
              onChange={(e) => selectDefaultModel(e.target.value)}
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
              {defaultEfforts.map((x) => (
                <option key={x}>{x}</option>
              ))}
            </select>
          </label>
          <label>
            默认并发
            <input
              type="number"
              min="1"
              max="72"
              step="1"
              value={prefs.default_concurrency}
              onChange={(e) =>
                setPrefs({ ...prefs, default_concurrency: +e.target.value })
              }
            />
          </label>
          <label>
            Agent 超时（秒）
            <input
              type="number"
              min={60}
              max={21600}
              step={1}
              value={prefs.agent_timeout_seconds}
              onChange={(e) =>
                setPrefs({ ...prefs, agent_timeout_seconds: +e.target.value })
              }
            />
          </label>
          <label>
            Verifier 超时（秒）
            <input
              type="number"
              min={60}
              max={7200}
              step={1}
              value={prefs.verifier_timeout_seconds}
              onChange={(e) =>
                setPrefs({ ...prefs, verifier_timeout_seconds: +e.target.value })
              }
            />
          </label>
          <label>
            基础设施重试次数（0 禁用）
            <input
              type="number"
              min={0}
              max={6}
              step={1}
              value={prefs.infrastructure_max_retries}
              onChange={(e) =>
                setPrefs({ ...prefs, infrastructure_max_retries: +e.target.value })
              }
            />
          </label>
          <label>
            最大步数（全部 Agent）
            <input
              type="number"
              min={10}
              max={500}
              step={1}
              value={prefs.agent_max_steps}
              onChange={(e) =>
                setPrefs({ ...prefs, agent_max_steps: +e.target.value })
              }
            />
          </label>
          <label>
            单个 Trial 费用熔断 (USD，0 禁用)
            <input
              type="number"
              min={0}
              step={0.5}
              value={prefs.trial_budget_usd}
              onChange={(e) =>
                setPrefs({ ...prefs, trial_budget_usd: +e.target.value })
              }
            />
          </label>
          <label>
            整个 Run 累计费用熔断 (USD，0 禁用)
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
