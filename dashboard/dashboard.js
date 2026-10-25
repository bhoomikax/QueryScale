"use strict";

const refreshButton = document.querySelector("#refresh-dashboard");
const analysisButton = document.querySelector("#run-analysis");
const trainModelButton = document.querySelector("#train-model");
const trainDemoButton = document.querySelector("#train-demo");
const trainingState = document.querySelector("#training-state");
const renderedAt = document.querySelector("#rendered-at");
const workspaceState = document.querySelector("#workspace-state");
const analysisState = document.querySelector("#analysis-state");
const JOB_POLL_MS = 2500;
const activePolls = new Map();

const LABEL_MEASURED = "measured_benchmark";
const LABEL_SYNTHETIC = "synthetic_demo";

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

function provenanceTag(labelSource) {
  if (labelSource === LABEL_MEASURED) {
    return makeTag("measured", "measured-trained");
  }
  if (labelSource === LABEL_SYNTHETIC) {
    return makeTag("synthetic", "synthetic-trained");
  }
  return null;
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
    if (recommendation.ml_label_source === LABEL_SYNTHETIC) {
      prediction.appendChild(makeTag("synthetic", "synthetic prediction"));
    } else {
      prediction.appendChild(makeTag("prediction", "prediction"));
    }
    const provenance = provenanceTag(recommendation.ml_label_source);
    if (provenance) {
      prediction.appendChild(provenance);
    }
    prediction.title = `Model ${recommendation.ml_model_version || "?"}. A prediction, not a measured result.`;
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
  if (recommendation.ranking_reason) {
    list.appendChild(makeElement("li", null, `Ranking: ${recommendation.ranking_reason}`));
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

function rankingLabel(recommendation) {
  if (recommendation.ranking_method !== "ml") {
    return "Rule-based ranking";
  }
  const provenance = recommendation.ml_provenance_label
    || (recommendation.ml_label_source === LABEL_SYNTHETIC ? "synthetic-trained" : "measured-trained");
  return `ML ranking (${provenance})`;
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
    score.textContent = `Rank ${recommendation.rank ?? "--"} · ${rankingLabel(recommendation)}`;
    score.title = recommendation.ranking_reason || recommendation.ml_status || "";

    const actions = makeElement("div", "recommendation-actions");
    const button = makeElement("button", "text-button", "Benchmark on test DB");
    button.type = "button";
    button.title = "Runs the configured label protocol on the isolated test database; the result joins the training set.";
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

function describeProtocol(protocol) {
  if (!protocol) {
    return "Benchmark protocol unavailable.";
  }
  const training = protocol.training_set || {};
  const trainingNote = training.workload_signature
    ? (training.matches_configured_protocol
      ? ` Training set uses this protocol (signature ${training.workload_signature}).`
      : ` Training set currently uses a different signature (${training.workload_signature}); new runs will not join it until it is retrained on this protocol.`)
    : " No measured samples yet.";
  return `Benchmark protocol v${protocol.protocol_version}: ${protocol.iterations} iterations × ${protocol.repeats} timed repeats after ${protocol.warm_up_passes} warm-up, seed ${protocol.seed}, ${(protocol.query_ids || []).length} query templates, test database ${protocol.test_database}. Dashboard benchmarks use these settings (BENCHMARK_ITERATIONS / BENCHMARK_REPEATS).${trainingNote}`;
}

function renderBenchmarkProtocol(protocol) {
  const note = document.querySelector("#benchmark-protocol");
  if (note) {
    note.textContent = describeProtocol(protocol);
  }
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
    const workload = latest.workload || {};
    const protocolNote = workload.iterations
      ? ` Protocol ${workload.iterations}×${workload.repeats}${workload.matches_label_protocol === false ? " (differs from the label protocol; excluded from training)" : workload.matches_label_protocol ? " (label protocol; usable for training)" : ""}.`
      : "";
    note.textContent = `Run ${latest.run_id} · ${latest.candidate_id} · finished ${latest.finished_at || "--"} · queries using the index: ${affected}.${protocolNote} Workload benefit is the share of median-weighted workload latency removed on the isolated test database.`;
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
  const hasPredictions = rows.some((row) => row.ml_predicted_benefit !== null && row.ml_predicted_benefit !== undefined);
  const maxPrediction = Math.max(...rows.map((row) => Number(row.ml_predicted_benefit) || 0), 0.01);
  container.replaceChildren();
  rows.forEach((recommendation) => {
    const row = makeChartRow(recommendation.candidate_id || "Candidate");
    row.append(makeBarLine("Rule", Number(recommendation.score) || 0, maximum, "bar-fill-score", ""));
    if (hasPredictions && recommendation.ml_predicted_benefit !== null && recommendation.ml_predicted_benefit !== undefined) {
      row.append(makeBarLine("Predicted", Number(recommendation.ml_predicted_benefit) * 100, maxPrediction * 100, "bar-fill-after", "%"));
    }
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
    const description = makeElement("dd");
    if (value instanceof Node) {
      description.appendChild(value);
    } else {
      description.textContent = value;
    }
    list.append(makeElement("dt", null, term), description);
  });
  return list;
}

function makeTable(headers, rows) {
  const table = makeElement("table", "ml-table");
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  headers.forEach((header) => headRow.appendChild(makeElement("th", null, header)));
  head.appendChild(headRow);
  const body = document.createElement("tbody");
  rows.forEach((cells) => {
    const row = document.createElement("tr");
    if (cells.selected) {
      row.className = "selected";
    }
    cells.values.forEach((value) => row.appendChild(makeElement("td", null, value)));
    body.appendChild(row);
  });
  table.append(head, body);
  return table;
}

function labelsWithTag(labelSource) {
  const holder = makeElement("span", null, labelSource || "--");
  const tag = provenanceTag(labelSource);
  if (tag) {
    holder.appendChild(tag);
  }
  return holder;
}

function comparisonRows(report) {
  if (!report || report.status !== "trained") {
    return [];
  }
  return Object.keys(report.holdout_metrics || {}).sort().map((name) => ({
    selected: name === report.selected_model,
    values: [
      `${name}${name === report.baseline_model ? " (baseline)" : ""}${name === report.selected_model ? " · selected" : ""}`,
      formatNumber((report.cv_mae || {})[name], 4),
      formatNumber(report.holdout_metrics[name].mae, 4),
      formatNumber(report.holdout_metrics[name].rmse, 4),
    ],
  }));
}

function renderMlEvaluation(model, evaluation, dataset) {
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
  const eligibility = model.eligibility || (dataset && dataset.eligibility) || {};
  const status = makeElement("section");
  status.append(
    makeElement("h3", null, "Ranking in use"),
    definitionList([
      ["Method", model.ranking_method === "ml" ? "ML model" : "Rule-based (fallback)"],
      ["Status", eligibility.status || "--"],
      ["Why", eligibility.reason || production.reason || "--"],
      ["Model version", production.model_version || "none"],
      ["Selected model", production.model_name || "--"],
      ["Labels", labelsWithTag(production.label_source)],
      ["Trained", production.trained_at || "--"],
      ["Samples / candidates", production.samples !== undefined && production.samples !== null ? `${production.samples} / ${production.groups}` : "--"],
    ]),
  );

  const data = makeElement("section");
  data.appendChild(makeElement("h3", null, "Training data (persisted samples)"));
  if (dataset && dataset.store) {
    const measured = dataset.store.by_label_source[LABEL_MEASURED] || {};
    const synthetic = dataset.store.by_label_source[LABEL_SYNTHETIC] || {};
    const required = (eligibility.measured_data && eligibility.measured_data.required) || {};
    const measuredValue = makeElement("span", null, `${measured.valid_samples ?? 0} valid · ${measured.excluded_samples ?? 0} excluded · ${measured.distinct_candidates ?? 0} candidates`);
    measuredValue.appendChild(makeTag("measured", "measured"));
    const syntheticValue = makeElement("span", null, `${synthetic.samples ?? 0} rows`);
    syntheticValue.appendChild(makeTag("synthetic", "synthetic"));
    const reasons = Object.entries(measured.exclusion_reasons || {}).map(([key, count]) => `${key}: ${count}`).join(", ");
    const measuredData = eligibility.measured_data || {};
    const comparableText = measuredData.measured_valid_samples !== undefined
      ? `${measuredData.measured_valid_samples} samples · ${measuredData.measured_candidates} candidates`
        + (measuredData.measured_other_protocol_samples ? ` · ${measuredData.measured_other_protocol_samples} valid sample(s) on other protocols excluded` : "")
        + (measuredData.comparable_workload_signature ? ` · signature ${measuredData.comparable_workload_signature}` : "")
      : "--";
    data.appendChild(definitionList([
      ["Measured", measuredValue],
      ["Comparable (one protocol)", comparableText],
      ["Synthetic", syntheticValue],
      ["Required for training", `${required.samples ?? "--"} measured samples across ${required.groups ?? "--"} candidates`],
      ["Eligible", eligibility.measured_data && eligibility.measured_data.eligible ? "yes" : "no"],
      ["Last sample", dataset.store.last_sample_at || "--"],
      ["History records", dataset.history_file ? String(dataset.history_file.records) : "--"],
      ["Excluded because", reasons || "nothing excluded"],
      ["Store", dataset.store.path || "--"],
    ]));
    data.appendChild(makeElement("p", "panel-note", "Synthetic rows exercise the pipeline only; they never count toward measured eligibility."));
  } else {
    data.appendChild(makeElement("p", "panel-note", "Dataset status unavailable."));
  }

  const report = evaluation && evaluation.production;
  const comparison = makeElement("section");
  const reportLabel = report && report.label_source === LABEL_SYNTHETIC ? "SYNTHETIC labels" : "measured labels";
  comparison.appendChild(makeElement("h3", null, `Model comparison · ${reportLabel}`));
  if (report && report.status === "trained") {
    comparison.appendChild(makeTable(["Model", "CV MAE (train)", "Held-out MAE", "Held-out RMSE"], comparisonRows(report)));
    comparison.appendChild(makeElement("p", "panel-note",
      `Latest training ${report.generated_at}: ${report.model_replaced ? "new model activated" : "model NOT activated"}; `
      + `active model ${report.active_model_version || "none"}. ${report.suitability_reason || ""} `
      + `Train/test ${report.train_samples}/${report.test_samples} rows from ${report.train_groups}/${report.test_groups} candidates; `
      + `selection: ${report.selection_basis || "--"}.`));
  } else if (report) {
    comparison.appendChild(definitionList([
      ["Latest attempt", `${report.generated_at}: ${report.status}`],
      ["Labeled samples", String(report.samples)],
      ["Distinct candidates", String(report.groups)],
      ["Required", `${report.minimums.samples} samples, ${report.minimums.groups} candidates`],
    ]));
    comparison.appendChild(makeElement("p", "panel-note", "Not enough measured benchmark labels for a held-out evaluation. Rule-based ranking stays authoritative."));
  } else {
    comparison.appendChild(makeElement("p", "panel-note", "No model has been trained on measured benchmark labels yet."));
  }

  const ranking = makeElement("section");
  ranking.appendChild(makeElement("h3", null, "Rule vs ML ranking (held-out)"));
  const rankingReport = report && report.ranking_comparison;
  if (rankingReport && rankingReport.groups_evaluated) {
    ranking.appendChild(makeTable(["Metric", "Rule score", "ML prediction"], [
      { values: [`NDCG@${rankingReport.k}`, formatNumber(rankingReport.rule.ndcg_at_k), formatNumber(rankingReport.ml.ndcg_at_k)] },
      { values: [`Precision@${rankingReport.k}`, formatNumber(rankingReport.rule.precision_at_k), formatNumber(rankingReport.ml.precision_at_k)] },
      { values: ["Spearman", formatNumber(rankingReport.rule.spearman), formatNumber(rankingReport.ml.spearman)] },
    ]));
    ranking.appendChild(makeElement("p", "panel-note", rankingReport.conclusion));
  } else {
    ranking.appendChild(makeElement("p", "panel-note", "Ranking comparison needs measured labels for several candidates on the same dataset."));
  }

  container.append(status, data, comparison, ranking);

  const notes = [];
  const demo = evaluation && evaluation.demo;
  if (demo && demo.status === "trained") {
    const selected = demo.holdout_metrics[demo.selected_model] || {};
    const baseline = demo.holdout_metrics[demo.baseline_model] || {};
    const note = makeElement("p", "ml-note",
      `Synthetic demo model (${demo.selected_model}): MAE ${formatNumber(selected.mae, 4)} vs baseline ${formatNumber(baseline.mae, 4)} on synthetic labels. `
      + "It only shows that the training pipeline runs. Synthetic evaluation is not evidence of real index recommendation quality.");
    note.prepend(makeTag("synthetic", "synthetic"));
    container.appendChild(note);
  }
  ((report && report.limitations) || []).forEach((item) => notes.push(item));
  notes.forEach((text) => container.appendChild(makeElement("p", "ml-note", text)));
}

function describeJob(job) {
  if (!job || job.status === "NEVER_RUN") {
    return "Analysis has not been run from the dashboard.";
  }
  if (job.status === "FAILED") {
    const code = job.error && job.error.code ? ` [${job.error.code}]` : "";
    return `Analysis failed${code}: ${(job.error && job.error.message) || "unknown error"}`;
  }
  if (job.status === "COMPLETED" && job.result) {
    const warnings = Array.isArray(job.result.warnings) && job.result.warnings.length
      ? ` · Warning: ${job.result.warnings.join(" ")}`
      : "";
    const counts = job.result.candidates_scored !== undefined
      ? ` · ${job.result.candidates_scored} scored candidates`
      : "";
    return `Analysis completed ${job.finished_at} · ${job.result.count} recommendations${counts} · ${job.result.ranking_method || "rule"} ranking · metadata from ${job.result.metadata_source}${warnings}`;
  }
  return `Analysis ${job.status.toLowerCase()} (${job.job_id})`;
}

function describeTrainingJob(job) {
  if (!job) {
    return "Training status unavailable.";
  }
  if (job.status === "FAILED") {
    const code = job.error && job.error.code ? ` [${job.error.code}]` : "";
    return `Training failed${code}: ${(job.error && job.error.message) || "unknown error"}`;
  }
  if (job.status === "COMPLETED" && job.result) {
    const result = job.result;
    const mode = result.mode === "synthetic_demo" ? "synthetic demo" : "measured";
    if (result.status === "insufficient_data") {
      const need = result.measured_eligibility && result.measured_eligibility.required;
      return `Training (${mode}) skipped: ${result.samples} samples across ${result.groups} candidates; need ${need ? `${need.samples} / ${need.groups}` : "more"}. Active model: ${result.active_model_version || "none"}.`;
    }
    return `Training (${mode}) ${result.status}: ${result.selected_model || "--"} ${result.model_version || ""} · suitable ${result.suitable ? "yes" : "no"} · ${result.model_replaced ? "activated" : "not activated"} · ${result.samples} samples`;
  }
  return `Training ${job.status.toLowerCase()} (${job.job_id})`;
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
          const workload = (finished.result && finished.result.workload) || {};
          const comparable = workload.matches_label_protocol === false ? " · not comparable with training labels" : "";
          state.textContent = `Measured: workload benefit ${formatPercent(finished.result.summary.workload_benefit, 2)}${comparable}`;
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

function setTrainingButtons(disabled) {
  [trainModelButton, trainDemoButton].forEach((button) => {
    if (button) {
      button.disabled = disabled;
    }
  });
}

async function submitTraining(syntheticDemo) {
  setTrainingButtons(true);
  if (trainingState) {
    trainingState.textContent = syntheticDemo ? "Training synthetic demo model…" : "Training from measured samples…";
  }
  try {
    const job = await submitJob("/api/ml/train", { synthetic_demo: syntheticDemo });
    pollJob(
      job.job_id,
      (current) => { if (trainingState) { trainingState.textContent = describeTrainingJob(current); } },
      (finished) => {
        setTrainingButtons(false);
        if (trainingState) {
          trainingState.textContent = describeTrainingJob(finished);
        }
        loadDashboardData();
      },
    );
  } catch (error) {
    setTrainingButtons(false);
    if (trainingState) {
      trainingState.textContent = error.message;
    }
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

function watchLatestTraining(jobsList) {
  const latest = Array.isArray(jobsList) ? jobsList[0] : null;
  if (!latest || !trainingState) {
    return;
  }
  trainingState.textContent = describeTrainingJob(latest);
  if (latest.status === "PENDING" || latest.status === "RUNNING") {
    setTrainingButtons(true);
    pollJob(
      latest.job_id,
      (current) => { trainingState.textContent = describeTrainingJob(current); },
      (finished) => {
        setTrainingButtons(false);
        trainingState.textContent = describeTrainingJob(finished);
        loadDashboardData();
      },
    );
  }
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

  const [history, model, evaluation, dataset, analysis, trainingJobs, protocol] = await Promise.all([
    fetchJson("/api/benchmark/history?limit=20").catch(() => []),
    fetchObject("/api/ml/model"),
    fetchObject("/api/ml/evaluation"),
    fetchObject("/api/ml/dataset"),
    fetchObject("/api/analysis/status"),
    fetchJson("/api/jobs?kind=training&limit=1").catch(() => []),
    fetchObject("/api/benchmark/protocol"),
  ]);
  renderBenchmarkRun(history);
  renderBenchmarkProtocol(protocol);
  renderMlEvaluation(model, evaluation, dataset);
  watchAnalysis(analysis);
  watchLatestTraining(trainingJobs);

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

if (trainModelButton) {
  trainModelButton.addEventListener("click", () => submitTraining(false));
}

if (trainDemoButton) {
  trainDemoButton.addEventListener("click", () => submitTraining(true));
}

if (renderedAt) {
  renderedAt.textContent = "Loading dashboard";
}

loadDashboardData();
