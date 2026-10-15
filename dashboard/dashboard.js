"use strict";

const refreshButton = document.querySelector("#refresh-dashboard");
const analysisButton = document.querySelector("#run-analysis");
const renderedAt = document.querySelector("#rendered-at");
const workspaceState = document.querySelector("#workspace-state");
const analysisState = document.querySelector("#analysis-state");
const JOB_POLL_MS = 2500;
const activePolls = new Map();

function setMetric(name, value) {
  const element = document.querySelector(`[data-metric="${name}"]`);
  if (element) {
    element.textContent = value;
  }
}

function makeElement(tag, className, text) {
  const element = document.createElement(tag);
  if (className) {
    element.className = className;
  }
  if (text !== undefined && text !== null) {
    element.textContent = text;
  }
  return element;
}

function makeTag(kind, label) {
  return makeElement("small", `tag tag-${kind}`, label);
}

function formatPercent(fraction, digits = 1) {
  return fraction === null || fraction === undefined || Number.isNaN(Number(fraction))
    ? "--"
    : `${(Number(fraction) * 100).toFixed(digits)}%`;
}

function formatBytes(bytes) {
  if (bytes === null || bytes === undefined) {
    return "--";
  }
  const value = Number(bytes);
  if (value >= 1024 * 1024) {
    return `${(value / (1024 * 1024)).toFixed(1)} MiB`;
  }
  return `${(value / 1024).toFixed(1)} KiB`;
}

function formatNumber(value, digits = 3) {
  return value === null || value === undefined ? "--" : Number(value).toFixed(digits);
}

function showListEmpty(container, title, detail) {
  container.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "empty-state";
  const mark = document.createElement("span");
  mark.className = "empty-state-mark";
  mark.setAttribute("aria-hidden", "true");
  mark.textContent = "+";
  const heading = document.createElement("p");
  heading.textContent = title;
  const explanation = document.createElement("span");
  explanation.textContent = detail;
  empty.append(mark, heading, explanation);
  container.appendChild(empty);
}

function recommendationMeta(recommendation) {
  const meta = makeElement("p", "recommendation-meta");

  const rule = makeElement("span", null, `Rule score ${Number(recommendation.rule_score ?? recommendation.score ?? 0).toFixed(1)}/100`);
  rule.appendChild(makeTag("heuristic", "heuristic"));
  meta.appendChild(rule);

  const prediction = makeElement("span");
  if (recommendation.ml_predicted_benefit !== null && recommendation.ml_predicted_benefit !== undefined) {
    prediction.textContent = `Predicted benefit ${formatPercent(recommendation.ml_predicted_benefit)}`;
    prediction.appendChild(makeTag("prediction", "prediction"));
  } else {
    prediction.textContent = "ML prediction not used";
    prediction.title = recommendation.ml_status || "No usable model; rule-based ranking.";
  }
  meta.appendChild(prediction);

  if (Array.isArray(recommendation.supporting_query_ids) && recommendation.supporting_query_ids.length) {
    meta.appendChild(makeElement("span", null, `Queries ${recommendation.supporting_query_ids.join(", ")}`));
  }
  if (recommendation.estimated_index_bytes !== null && recommendation.estimated_index_bytes !== undefined) {
    const storage = makeElement("span", null, `Size ${formatBytes(recommendation.estimated_index_bytes)}`);
    storage.appendChild(makeTag("estimate", "estimate"));
    meta.appendChild(storage);
  }
  if (recommendation.write_ratio !== null && recommendation.write_ratio !== undefined) {
    meta.appendChild(makeElement("span", null, `Table writes ${formatPercent(recommendation.write_ratio)} of captured executions`));
  }
  return meta;
}

function recommendationDetails(recommendation) {
  const details = makeElement("details", "recommendation-details");
  details.appendChild(makeElement("summary", null, "Explanation and costs"));
  const list = document.createElement("ul");
  const add = (prefix, items) => {
    (items || []).forEach((item) => list.appendChild(makeElement("li", null, prefix ? `${prefix}: ${item}` : item)));
  };
  if (recommendation.explanation) {
    list.appendChild(makeElement("li", null, recommendation.explanation));
  }
  add("Evidence", recommendation.positive_evidence);
  add("Cost", recommendation.costs);
  add("Drawback", recommendation.drawbacks);
  if (Array.isArray(recommendation.unavailable_metrics) && recommendation.unavailable_metrics.length) {
    list.appendChild(makeElement("li", null, `Unavailable: ${recommendation.unavailable_metrics.join(", ")}`));
  }
  if (recommendation.recommended_sql) {
    list.appendChild(makeElement("li", null, `Test-database SQL: ${recommendation.recommended_sql}`));
  }
  details.appendChild(list);
  return details;
}

function renderRecommendations(recommendations) {
  const container = document.querySelector("#recommendation-list");
  if (!container) {
    return;
  }
  if (!Array.isArray(recommendations) || recommendations.length === 0) {
    showListEmpty(container, "No recommendations loaded", "Run the analysis pipeline to populate this view.");
    return;
  }

  container.replaceChildren();
  recommendations.slice(0, 6).forEach((recommendation) => {
    const item = document.createElement("article");
    item.className = "recommendation-item";

    const heading = document.createElement("div");
    heading.className = "recommendation-heading";
    const candidate = document.createElement("strong");
    candidate.textContent = recommendation.candidate_id || "Candidate";
    const priority = document.createElement("span");
    priority.className = `priority priority-${String(recommendation.priority || "").toLowerCase()}`;
    priority.textContent = recommendation.priority || "Unranked";
    heading.append(candidate, priority);

    const target = document.createElement("p");
    target.className = "recommendation-target";
    target.textContent = `${recommendation.table || "Unknown table"} / ${(recommendation.columns || []).join(", ")}`;

    const reason = document.createElement("p");
    reason.className = "recommendation-reason";
    reason.textContent = recommendation.reason || "No reason provided.";
    reason.title = recommendation.reason || "";

    const score = document.createElement("span");
    score.className = "recommendation-score";
    score.textContent = `Rank ${recommendation.rank ?? "--"} · ${recommendation.ranking_method === "ml" ? "ML ranking" : "Rule-based ranking"}`;

    const actions = makeElement("div", "recommendation-actions");
    const button = makeElement("button", "text-button", "Benchmark on test DB");
    button.type = "button";
    const state = makeElement("span", "job-state");
    button.addEventListener("click", () => submitBenchmark(recommendation.candidate_id, button, state));
    actions.append(button, state);

    item.append(heading, target, reason, recommendationMeta(recommendation), recommendationDetails(recommendation), score, actions);
    container.appendChild(item);
  });
}

async function fetchJson(url) {
  const response = await fetch(url);
  if (response.status === 404) {
    return [];
  }
  if (!response.ok) {
    throw new Error(`Request failed: ${url}`);
  }
  return response.json();
}

async function fetchObject(url) {
  try {
    const value = await fetchJson(url);
    return Array.isArray(value) ? null : value;
  } catch (error) {
    console.error(error);
    return null;
  }
}

function updateSummary(queries, slowQueries, recommendations, benchmarkResults) {
  setMetric("queries", queries.length);
  setMetric("slow", slowQueries.length);
  setMetric("recommendations", recommendations.length);
  setMetric(
    "high-priority",
    recommendations.filter((recommendation) => String(recommendation.priority).toLowerCase() === "high").length,
  );

  if (!benchmarkResults.length) {
    setMetric("before", "--");
    setMetric("after", "--");
    setMetric("speedup", "--");
    setMetric("improvement", "--");
    return;
  }

  const executionCount = benchmarkResults.reduce(
    (sum, row) => sum + (Number(row.execution_count) || 0),
    0,
  );
  if (!executionCount) {
    setMetric("before", "--");
    setMetric("after", "--");
    setMetric("speedup", "--");
    setMetric("improvement", "--");
    return;
  }
  const before = benchmarkResults.reduce(
    (sum, row) => sum + (Number(row.before_avg_ms) || 0) * (Number(row.execution_count) || 0),
    0,
  ) / executionCount;
  const after = benchmarkResults.reduce(
    (sum, row) => sum + (Number(row.after_avg_ms) || 0) * (Number(row.execution_count) || 0),
    0,
  ) / executionCount;
  setMetric("before", before.toFixed(1));
  setMetric("after", after.toFixed(1));
  setMetric("speedup", after ? `${(before / after).toFixed(2)}x` : "--");
  setMetric("improvement", before ? `${(((before - after) / before) * 100).toFixed(1)}%` : "--");
}

function renderBenchmarkRun(history) {
  const note = document.querySelector("#benchmark-run");
  const latest = Array.isArray(history) ? history.find((run) => run.status === "COMPLETED") : null;
  if (!latest) {
    setMetric("workload-benefit", "--");
    setMetric("index-size", "--");
    setMetric("write-overhead", "--");
    if (note) {
      note.textContent = "No completed benchmark run recorded.";
    }
    return;
  }
  const summary = latest.summary || {};
  setMetric("workload-benefit", formatPercent(summary.workload_benefit, 2));
  setMetric("index-size", latest.index_size_bytes === null || latest.index_size_bytes === undefined ? "unavailable" : formatBytes(latest.index_size_bytes));
  const overhead = latest.write_overhead || {};
  const parts = ["insert", "update", "delete"]
    .filter((op) => overhead[`${op}_overhead_pct`] !== null && overhead[`${op}_overhead_pct`] !== undefined)
    .map((op) => `${op.toUpperCase()} ${Number(overhead[`${op}_overhead_pct`]) >= 0 ? "+" : ""}${Number(overhead[`${op}_overhead_pct`]).toFixed(1)}%`);
  setMetric("write-overhead", parts.length ? parts.join(" · ") : "unavailable");
  if (note) {
    const affected = (summary.affected_queries || []).join(", ") || "none";
    note.textContent = `Run ${latest.run_id} · ${latest.candidate_id} · finished ${latest.finished_at || "--"} · queries using the index: ${affected}. Workload benefit is the share of median-weighted workload latency removed on the isolated test database.`;
  }
}

function showChartEmpty(container, message) {
  if (!container) {
    return;
  }
  container.replaceChildren();
  const empty = document.createElement("div");
  empty.className = "chart-empty";
  empty.textContent = message;
  container.appendChild(empty);
}

function makeBarLine(label, value, maximum, fillClass, suffix) {
  const line = document.createElement("div");
  line.className = "bar-line";

  const name = document.createElement("span");
  name.className = "bar-name";
  name.textContent = label;

  const track = document.createElement("span");
  track.className = "bar-track";
  const fill = document.createElement("span");
  fill.className = `bar-fill ${fillClass}`;
  fill.style.width = `${Math.max(0, Math.min(100, (value / maximum) * 100))}%`;
  track.appendChild(fill);

  const formattedValue = document.createElement("span");
  formattedValue.className = "bar-value";
  formattedValue.textContent = `${value.toFixed(1)}${suffix}`;

  line.append(name, track, formattedValue);
  return line;
}

function makeChartRow(label) {
  const row = document.createElement("div");
  row.className = "chart-row";
  const rowLabel = document.createElement("span");
  rowLabel.className = "chart-row-label";
  rowLabel.textContent = label;
  row.appendChild(rowLabel);
  return row;
}

function renderPerformanceChart(results) {
  const container = document.querySelector("#performance-chart");
  if (!Array.isArray(results) || results.length === 0) {
    showChartEmpty(container, "Benchmark data will appear here.");
    return;
  }

  const rows = [...results]
    .sort((left, right) => Number(Boolean(right.affected)) - Number(Boolean(left.affected)))
    .slice(0, 6);
  const maximum = Math.max(...rows.map((row) => Number(row.before_avg_ms) || 0), 1);
  container.replaceChildren();
  rows.forEach((result) => {
    const row = makeChartRow(`${result.query_id || "Query"}${result.affected ? " · uses index" : ""}`);
    row.append(
      makeBarLine("Before", Number(result.before_avg_ms) || 0, maximum, "bar-fill-before", " ms"),
      makeBarLine("After", Number(result.after_avg_ms) || 0, maximum, "bar-fill-after", " ms"),
    );
    if (result.before_rows_examined !== null && result.before_rows_examined !== undefined
      && result.after_rows_examined !== null && result.after_rows_examined !== undefined) {
      row.appendChild(makeElement(
        "span",
        "chart-row-value",
        `Rows examined ${Number(result.before_rows_examined).toFixed(0)} → ${Number(result.after_rows_examined).toFixed(0)}`,
      ));
    }
    container.appendChild(row);
  });
}

function renderRecommendationChart(recommendations) {
  const container = document.querySelector("#recommendation-chart");
  if (!Array.isArray(recommendations) || recommendations.length === 0) {
    showChartEmpty(container, "Recommendation scores will appear here.");
    return;
  }

  const rows = recommendations.slice(0, 6);
  const maximum = Math.max(...rows.map((row) => Number(row.score) || 0), 1);
  container.replaceChildren();
  rows.forEach((recommendation) => {
    const row = makeChartRow(recommendation.candidate_id || "Candidate");
    row.append(makeBarLine("Rule", Number(recommendation.score) || 0, maximum, "bar-fill-score", ""));
    container.appendChild(row);
  });
}

function renderFrequencyChart(queries) {
  const container = document.querySelector("#frequency-chart");
  if (!Array.isArray(queries) || queries.length === 0) {
    showChartEmpty(container, "Workload frequency will appear here.");
    return;
  }

  const grouped = new Map();
  queries.forEach((query) => {
    const current = grouped.get(query.query_id) || { count: 0, totalMs: 0 };
    current.count += 1;
    current.totalMs += Number(query.execution_time_ms) || 0;
    grouped.set(query.query_id, current);
  });
  const rows = [...grouped.entries()].sort((left, right) => right[1].count - left[1].count).slice(0, 8);
  const maximum = Math.max(...rows.map(([, value]) => value.count), 1);
  container.replaceChildren();
  rows.forEach(([queryId, value]) => {
    const row = makeChartRow(queryId || "Query");
    row.append(makeBarLine("Runs", value.count, maximum, "bar-fill-frequency", ""));
    const average = document.createElement("span");
    average.className = "chart-row-value";
    average.textContent = `Average latency ${ (value.totalMs / value.count).toFixed(1) } ms`;
    row.appendChild(average);
    container.appendChild(row);
  });
}

function definitionList(entries) {
  const list = document.createElement("dl");
  entries.forEach(([term, value]) => {
    list.append(makeElement("dt", null, term), makeElement("dd", null, value));
  });
  return list;
}

function renderMlEvaluation(model, evaluation) {
  const container = document.querySelector("#ml-evaluation");
  if (!container) {
    return;
  }
  container.replaceChildren();
  if (!model) {
    container.appendChild(makeElement("p", "ml-note", "Model status unavailable."));
    return;
  }

  const production = model.production || {};
  const status = makeElement("section");
  status.append(
    makeElement("h3", null, "Ranking in use"),
    definitionList([
      ["Method", model.ranking_method === "ml" ? "ML model" : "Rule-based"],
      ["Model version", production.model_version || "none"],
      ["Selected model", production.model_name || "--"],
      ["Labels", production.label_source || "--"],
      ["Trained", production.trained_at || "--"],
    ]),
    makeElement("p", "panel-note", production.reason || ""),
  );

  const report = evaluation && evaluation.production;
  const errors = makeElement("section");
  errors.appendChild(makeElement("h3", null, "Held-out error, measured labels"));
  if (report && report.status === "trained") {
    const selected = report.holdout_metrics[report.selected_model] || {};
    const baseline = report.holdout_metrics[report.baseline_model] || {};
    errors.appendChild(definitionList([
      [`MAE ${report.selected_model}`, formatNumber(selected.mae, 4)],
      [`RMSE ${report.selected_model}`, formatNumber(selected.rmse, 4)],
      ["MAE mean baseline", formatNumber(baseline.mae, 4)],
      ["Train / test samples", `${report.train_samples} / ${report.test_samples}`],
      ["Beats baseline", report.suitable ? "yes" : "no"],
    ]));
  } else if (report) {
    errors.appendChild(definitionList([
      ["Labeled samples", String(report.samples)],
      ["Distinct candidates", String(report.groups)],
      ["Required", `${report.minimums.samples} samples, ${report.minimums.groups} candidates`],
    ]));
    errors.appendChild(makeElement("p", "panel-note", "Not enough measured benchmark labels for a held-out evaluation."));
  } else {
    errors.appendChild(makeElement("p", "panel-note", "No model has been trained on measured benchmark labels yet."));
  }

  const ranking = makeElement("section");
  ranking.appendChild(makeElement("h3", null, "Rule vs ML ranking"));
  const comparison = report && report.ranking_comparison;
  if (comparison && comparison.groups_evaluated) {
    ranking.appendChild(definitionList([
      [`NDCG@${comparison.k} rule`, formatNumber(comparison.rule.ndcg_at_k)],
      [`NDCG@${comparison.k} ML`, formatNumber(comparison.ml.ndcg_at_k)],
      [`Precision@${comparison.k} rule`, formatNumber(comparison.rule.precision_at_k)],
      [`Precision@${comparison.k} ML`, formatNumber(comparison.ml.precision_at_k)],
    ]));
    ranking.appendChild(makeElement("p", "panel-note", comparison.conclusion));
  } else {
    ranking.appendChild(makeElement("p", "panel-note", "Ranking comparison needs measured labels for several candidates on the same dataset."));
  }

  container.append(status, errors, ranking);

  const notes = [];
  const demo = evaluation && evaluation.demo;
  if (demo && demo.status === "trained") {
    const selected = demo.holdout_metrics[demo.selected_model] || {};
    const baseline = demo.holdout_metrics[demo.baseline_model] || {};
    notes.push(
      `Synthetic demo model (${demo.selected_model}): MAE ${formatNumber(selected.mae, 4)} vs baseline ${formatNumber(baseline.mae, 4)} on synthetic labels. `
      + "It only shows that the training pipeline runs and is never used to rank recommendations.",
    );
  }
  ((report && report.limitations) || []).forEach((item) => notes.push(item));
  notes.forEach((text) => container.appendChild(makeElement("p", "ml-note", text)));
}

function describeJob(job) {
  if (!job || job.status === "NEVER_RUN") {
    return "Analysis has not been run from the dashboard.";
  }
  if (job.status === "FAILED") {
    return `Analysis failed: ${(job.error && job.error.message) || "unknown error"}`;
  }
  if (job.status === "COMPLETED" && job.result) {
    return `Analysis completed ${job.finished_at} · ${job.result.count} recommendations · ${job.result.ranking_method || "rule"} ranking · metadata from ${job.result.metadata_source}`;
  }
  return `Analysis ${job.status.toLowerCase()} (${job.job_id})`;
}

function pollJob(jobId, onUpdate, onDone) {
  if (activePolls.has(jobId)) {
    return;
  }
  const tick = async () => {
    const job = await fetchObject(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!job) {
      activePolls.delete(jobId);
      onDone(null);
      return;
    }
    onUpdate(job);
    if (job.status === "COMPLETED" || job.status === "FAILED") {
      activePolls.delete(jobId);
      onDone(job);
      return;
    }
    activePolls.set(jobId, window.setTimeout(tick, JOB_POLL_MS));
  };
  activePolls.set(jobId, window.setTimeout(tick, 0));
}

async function submitJob(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 409) {
    const active = payload.detail && payload.detail.active_job;
    throw new Error(active ? `Busy: ${active.kind} job ${active.job_id} is ${active.status.toLowerCase()}` : "Another job is running");
  }
  if (!response.ok) {
    const detail = payload.detail;
    throw new Error(typeof detail === "string" ? detail : (detail && detail.message) || `Request failed (${response.status})`);
  }
  return payload;
}

async function submitBenchmark(candidateId, button, state) {
  button.disabled = true;
  state.textContent = "Submitting benchmark…";
  try {
    const job = await submitJob(`/api/benchmark/${encodeURIComponent(candidateId)}?background=true`);
    pollJob(
      job.job_id,
      (current) => { state.textContent = `Benchmark ${current.status.toLowerCase()}…`; },
      (finished) => {
        button.disabled = false;
        if (finished && finished.status === "COMPLETED") {
          state.textContent = `Measured: workload benefit ${formatPercent(finished.result.summary.workload_benefit, 2)}`;
          loadDashboardData();
        } else {
          state.textContent = `Benchmark failed: ${(finished && finished.error && finished.error.message) || "unknown error"}`;
        }
      },
    );
  } catch (error) {
    button.disabled = false;
    state.textContent = error.message;
  }
}

async function submitAnalysis() {
  if (!analysisButton) {
    return;
  }
  analysisButton.disabled = true;
  try {
    const job = await submitJob("/api/analysis", {});
    watchAnalysis(job);
  } catch (error) {
    analysisButton.disabled = false;
    if (analysisState) {
      analysisState.textContent = error.message;
    }
  }
}

function watchAnalysis(job) {
  if (analysisState) {
    analysisState.textContent = describeJob(job);
  }
  if (!job || !job.job_id || job.status === "COMPLETED" || job.status === "FAILED") {
    if (analysisButton) {
      analysisButton.disabled = false;
    }
    return;
  }
  if (analysisButton) {
    analysisButton.disabled = true;
  }
  pollJob(
    job.job_id,
    (current) => { if (analysisState) { analysisState.textContent = describeJob(current); } },
    (finished) => {
      if (analysisButton) {
        analysisButton.disabled = false;
      }
      if (analysisState) {
        analysisState.textContent = describeJob(finished);
      }
      if (finished && finished.status === "COMPLETED") {
        loadDashboardData();
      }
    },
  );
}

async function loadDashboardData() {
  try {
    const [queries, slowQueries, recommendations, benchmarkResults] = await Promise.all([
      fetchJson("/api/queries"),
      fetchJson("/api/queries/slow"),
      fetchJson("/api/recommendations"),
      fetchJson("/api/benchmark/results"),
    ]);
    updateSummary(queries, slowQueries, recommendations, benchmarkResults);
    renderRecommendations(recommendations);
    renderPerformanceChart(benchmarkResults);
    renderRecommendationChart(recommendations);
    renderFrequencyChart(queries);
    if (workspaceState) {
      workspaceState.textContent = queries.length || recommendations.length || benchmarkResults.length
        ? "Live data"
        : "Awaiting artifacts";
    }
  } catch (error) {
    setMetric("queries", "--");
    setMetric("slow", "--");
    setMetric("recommendations", "--");
    setMetric("high-priority", "--");
    showListEmpty(document.querySelector("#recommendation-list"), "API unavailable", "Start the local application to load dashboard data.");
    showChartEmpty(document.querySelector("#performance-chart"), "API unavailable.");
    showChartEmpty(document.querySelector("#recommendation-chart"), "API unavailable.");
    showChartEmpty(document.querySelector("#frequency-chart"), "API unavailable.");
    if (workspaceState) {
      workspaceState.textContent = "API unavailable";
    }
    console.error(error);
  }

  const [history, model, evaluation, analysis] = await Promise.all([
    fetchJson("/api/benchmark/history?limit=20").catch(() => []),
    fetchObject("/api/ml/model"),
    fetchObject("/api/ml/evaluation"),
    fetchObject("/api/analysis/status"),
  ]);
  renderBenchmarkRun(history);
  renderMlEvaluation(model, evaluation);
  watchAnalysis(analysis);

  if (renderedAt) {
    renderedAt.textContent = `Updated ${new Date().toLocaleTimeString()}`;
  }
}

showChartEmpty(document.querySelector("#performance-chart"), "Benchmark data will appear here.");
showChartEmpty(document.querySelector("#recommendation-chart"), "Recommendation scores will appear here.");
showChartEmpty(document.querySelector("#frequency-chart"), "Workload frequency will appear here.");

window.QueryScaleDashboard = Object.freeze({
  loadDashboardData,
  renderPerformanceChart,
  renderRecommendationChart,
  renderFrequencyChart,
  renderMlEvaluation,
  renderBenchmarkRun,
});

if (refreshButton) {
  refreshButton.addEventListener("click", () => window.location.reload());
}

if (analysisButton) {
  analysisButton.addEventListener("click", submitAnalysis);
}

if (renderedAt) {
  renderedAt.textContent = "Loading dashboard";
}

loadDashboardData();
