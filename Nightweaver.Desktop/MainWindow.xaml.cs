using System.Diagnostics;
using System.Collections.ObjectModel;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Threading;
using Microsoft.Win32;

namespace Nightweaver.Desktop;

public partial class MainWindow : Window
{
    private readonly DispatcherTimer refreshTimer = new() { Interval = TimeSpan.FromSeconds(3) };
    private readonly string? sourceRoot = FindSourceRoot();
    private readonly string python = Argument("--python") ?? Environment.GetEnvironmentVariable("NIGHTWEAVER_PYTHON")
        ?? Environment.GetEnvironmentVariable("ROOKLINE_PYTHON") ?? "python.exe";
    private readonly string dataRoot = FindDataRoot();
    private string? visibleRun;
    private long lastEvent;
    private readonly ObservableCollection<Activity> history = [];
    private bool refreshing;
    private bool updatingList;
    private bool working;
    private bool rendering;
    private bool selectionPending;
    private readonly Dictionary<string, string> guidanceDrafts = [];
    private RunRow[] knownRuns = [];

    public MainWindow()
    {
        InitializeComponent();
        Timeline.ItemsSource = history;
        refreshTimer.Tick += async (_, _) => await RefreshAsync();
        Loaded += async (_, _) =>
        {
            await RefreshAsync();
            refreshTimer.Start();
        };
        Closed += (_, _) => refreshTimer.Stop();
    }

    private static string? FindSourceRoot()
    {
        string? configured = Environment.GetEnvironmentVariable("NIGHTWEAVER_ROOT")
            ?? Environment.GetEnvironmentVariable("ROOKLINE_ROOT");
        if (!string.IsNullOrWhiteSpace(configured))
            return configured;
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
            if (File.Exists(Path.Combine(dir.FullName, "nightweaver", "cli.py")))
                return dir.FullName;
        return null;
    }

    private static string FindDataRoot()
    {
        if (Argument("--home") is string home)
            return Path.GetFullPath(home);
        string? configured = Environment.GetEnvironmentVariable("NIGHTWEAVER_HOME")
            ?? Environment.GetEnvironmentVariable("ROOKLINE_HOME");
        if (!string.IsNullOrWhiteSpace(configured))
            return configured;
        string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string legacy = Path.Combine(local, "Rookline");
        return File.Exists(Path.Combine(legacy, "runs.sqlite3")) ? legacy : Path.Combine(local, "Nightweaver");
    }

    private static string? Argument(string name)
    {
        var args = Environment.GetCommandLineArgs();
        int index = Array.IndexOf(args, name);
        return index >= 0 && index + 1 < args.Length ? args[index + 1] : null;
    }

    private async Task<JsonElement> CallAsync(params string[] arguments)
    {
        var start = new ProcessStartInfo(python)
        {
            CreateNoWindow = true,
            UseShellExecute = false,
            RedirectStandardError = true,
            RedirectStandardOutput = true
        };
        if (sourceRoot is not null)
            start.WorkingDirectory = sourceRoot;
        foreach (string arg in new[] { "-m", "nightweaver", "--home", dataRoot }.Concat(arguments))
            start.ArgumentList.Add(arg);
        using var process = Process.Start(start) ?? throw new InvalidOperationException("Python did not start.");
        var output = process.StandardOutput.ReadToEndAsync();
        var errors = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync();
        if (process.ExitCode != 0)
            throw new InvalidOperationException((await errors).Trim());
        using var result = JsonDocument.Parse(await output);
        return result.RootElement.Clone();
    }

    private async Task RefreshAsync(string? select = null)
    {
        if (refreshing)
            return;
        refreshing = true;
        try
        {
            knownRuns = (await CallAsync("list", "--compact")).EnumerateArray().Select(RunRow.Parse).ToArray();
            FilterRuns(select ?? (RunList.SelectedItem as RunRow)?.Id);
            await RenderSelectionAsync();
            if (!working)
                Status.Text = dataRoot;
        }
        catch (Exception error)
        {
            Status.Text = error.Message;
        }
        finally
        {
            refreshing = false;
        }
    }

    private void FilterRuns(string? selected)
    {
        string term = RunSearch.Text.Trim();
        var rows = knownRuns.Where(r => string.Join(" ", r.Id, r.Title, r.Subtitle)
            .Contains(term, StringComparison.OrdinalIgnoreCase)).ToArray();
        updatingList = true;
        try
        {
            if (RunList.ItemsSource is not RunRow[] old || !old.SequenceEqual(rows))
                RunList.ItemsSource = rows;
            RunList.SelectedItem = rows.FirstOrDefault(r => r.Id == selected) ?? rows.FirstOrDefault();
            RunCount.Text = term.Length == 0 ? $"{rows.Length} runs" : $"{rows.Length} of {knownRuns.Length} runs";
        }
        finally { updatingList = false; }
    }

    private async void SearchChanged(object sender, TextChangedEventArgs e)
    {
        if (!IsLoaded) return;
        FilterRuns((RunList.SelectedItem as RunRow)?.Id);
        try { await RenderSelectionAsync(); }
        catch (Exception error) { Status.Text = error.Message; }
    }

    private void SearchKeyDown(object sender, System.Windows.Input.KeyEventArgs e)
    {
        if (e.Key == System.Windows.Input.Key.Escape) { RunSearch.Clear(); e.Handled = true; }
    }

    private async Task RenderSelectionAsync()
    {
        if (rendering)
        {
            selectionPending = true;
            return;
        }
        rendering = true;
        try
        {
        if (RunList.SelectedItem is not RunRow row)
        {
            if (visibleRun is not null) guidanceDrafts[visibleRun] = FeedbackText.Text;
            visibleRun = null;
            lastEvent = 0;
            history.Clear();
            FeedbackText.Clear();
            RunTitle.Text = knownRuns.Length == 0 ? "No runs yet" : "No matching runs";
            RunSummary.Text = knownRuns.Length == 0 ? "Start a OneShot or open a plan." : "Change your search or press Escape in the search field.";
            Branch.Visibility = HandoffButton.Visibility = Visibility.Collapsed;
            FeedbackText.IsEnabled = FeedbackButton.IsEnabled = EvidenceButton.IsEnabled = ReportButton.IsEnabled =
                PauseButton.IsEnabled = ResumeButton.IsEnabled = false;
            return;
        }
        if (visibleRun != row.Id)
        {
            if (visibleRun is not null) guidanceDrafts[visibleRun] = FeedbackText.Text;
            FeedbackText.Text = guidanceDrafts.GetValueOrDefault(row.Id, "");
            visibleRun = row.Id;
            lastEvent = 0;
            history.Clear();
        }
        RunTitle.Text = row.Title;
        RunSummary.Text = $"{row.Subtitle}\n{row.Current}".TrimEnd();
        Branch.Text = $"{row.ResultBranch}    {row.Accepted}";
        Branch.Visibility = Visibility.Visible;
        HandoffButton.Visibility = Visibility.Visible;
        EvidenceButton.IsEnabled = true;
        ReportButton.IsEnabled = !working;
        FeedbackText.IsEnabled = FeedbackButton.IsEnabled = !working && row.Phase != "done";
        PauseButton.IsEnabled = !working && (row.Active || row.Queued) && !row.Pause && row.Phase is not ("done" or "blocked");
        ResumeButton.IsEnabled = !working && !row.Active && row.Phase != "done" && (!row.Waiting || row.Pause);
        ResumeButton.Content = row.Phase == "blocked" ? "Retry" : "Resume";

        bool changed = false;
        while (true)
        {
            var page = (await CallAsync("events", row.Id, "--after", lastEvent.ToString(), "--limit", "500"))
                .EnumerateArray().Select(Activity.Parse).ToArray();
            if ((RunList.SelectedItem as RunRow)?.Id != row.Id)
                return;
            foreach (var item in page) history.Add(item);
            changed |= page.Length > 0;
            if (page.Length > 0)
                lastEvent = page[^1].Seq;
            if (page.Length < 500)
                break;
        }
        if (changed && history.Count > 0 && FollowActivity.IsChecked == true)
            Timeline.ScrollIntoView(history[^1]);
        }
        finally
        {
            rendering = false;
            if (selectionPending)
            {
                selectionPending = false;
                await RenderSelectionAsync();
            }
        }
    }

    private async void Run_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (updatingList)
            return;
        try { await RenderSelectionAsync(); }
        catch (Exception error) { Status.Text = error.Message; }
    }

    private string? PickPlan(string title)
    {
        var picker = new OpenFileDialog { Title = title, Filter = "Nightweaver JSON (*.json)|*.json" };
        return picker.ShowDialog(this) == true ? picker.FileName : null;
    }

    private async void Add_Click(object sender, RoutedEventArgs e)
    {
        if (working || PickPlan("Select a Nightweaver plan") is not string path)
            return;
        await PerformAsync("Starting plan…", "add", path, "--start");
    }

    private async void OneShot_Click(object sender, RoutedEventArgs e)
    {
        if (working)
            return;
        var dialog = new GoalWindow(path => CallAsync("oneshot", path, "Preview committed checks", "--preview")) { Owner = this };
        if (dialog.ShowDialog() == true)
            await PerformAsync("Planning milestones…", dialog.Arguments());
    }

    private async void Pause_Click(object sender, RoutedEventArgs e)
    {
        if (!working && RunList.SelectedItem is RunRow row)
            await PerformAsync("Pausing run…", "pause", row.Id);
    }

    private async void Resume_Click(object sender, RoutedEventArgs e)
    {
        if (!working && RunList.SelectedItem is RunRow row)
            await PerformAsync("Starting runner…", row.Phase == "blocked"
                ? ["resume", row.Id, "--retry"] : ["resume", row.Id]);
    }

    private async void Handoff_Click(object sender, RoutedEventArgs e)
    {
        if (RunList.SelectedItem is not RunRow row)
            return;
        try
        {
            var result = await CallAsync("handoff", row.Id);
            Clipboard.SetText(result.GetRawText());
            Status.Text = $"Copied handoff for {row.Title}.";
        }
        catch (Exception error) { Status.Text = error.Message; }
    }

    private void Evidence_Click(object sender, RoutedEventArgs e)
    {
        if (RunList.SelectedItem is not RunRow row)
            return;
        try { Process.Start(new ProcessStartInfo(Path.Combine(dataRoot, "runs", row.Id)) { UseShellExecute = true }); }
        catch (Exception error) { Status.Text = error.Message; }
    }

    private async void Report_Click(object sender, RoutedEventArgs e)
    {
        if (working || RunList.SelectedItem is not RunRow row)
            return;
        working = true;
        try
        {
            var result = await CallAsync("report", row.Id, "--include-images");
            Process.Start(new ProcessStartInfo(result.GetProperty("report").GetString()!) { UseShellExecute = true });
        }
        catch (Exception error) { Status.Text = error.Message; }
        finally { working = false; }
    }

    private async void Feedback_Click(object sender, RoutedEventArgs e)
    {
        if (working || RunList.SelectedItem is not RunRow row || visibleRun != row.Id || string.IsNullOrWhiteSpace(FeedbackText.Text))
            return;
        working = true;
        try
        {
            string submitted = FeedbackText.Text;
            await CallAsync("feedback", row.Id, submitted);
            if (visibleRun == row.Id && FeedbackText.Text == submitted) FeedbackText.Clear();
            if (guidanceDrafts.GetValueOrDefault(row.Id) == submitted) guidanceDrafts.Remove(row.Id);
            Status.Text = "Guidance saved for the next attempt. Paused runs remain paused.";
        }
        catch (Exception error) { Status.Text = error.Message; }
        finally { working = false; }
    }

    private async Task PerformAsync(string message, params string[] arguments)
    {
        working = true;
        Status.Text = message;
        string? id = null;
        try
        {
            var result = await CallAsync(arguments);
            id = result.GetProperty("run_id").GetString();
        }
        catch (Exception error)
        {
            MessageBox.Show(this, error.Message, "Nightweaver", MessageBoxButton.OK, MessageBoxImage.Error);
        }
        finally
        {
            working = false;
        }
        await RefreshAsync(id);
    }

    private sealed record RunRow(string Id, string Title, string Phase, bool Pause, bool Active, bool Queued,
        bool Waiting, string Subtitle, string Current, string Accepted, string ResultBranch)
    {
        public static RunRow Parse(JsonElement data)
        {
            var spec = data.GetProperty("spec");
            var state = data.GetProperty("state");
            // A goal pursuit counts the milestones of waves a revert discarded in its plan, but they are not part of the build.
            int lost = state.TryGetProperty("discarded", out var discarded) && discarded.TryGetInt32(out int dropped) ? dropped : 0;
            bool pursuit = state.TryGetProperty("pursuit", out var pursuing) && pursuing.ValueKind == JsonValueKind.True;
            int step = Math.Max(0, state.GetProperty("step").GetInt32() - lost);
            int count = Math.Max(0, data.GetProperty("total_milestones").GetInt32() - lost);
            string phase = Get(state, "phase");
            bool pause = data.GetProperty("pause").GetBoolean();
            bool supervised = data.TryGetProperty("guard_active", out var guard) && guard.GetBoolean();
            bool active = data.GetProperty("runner_active").GetBoolean();
            bool queued = state.TryGetProperty("dispatch", out var dispatch) && dispatch.GetBoolean();
            var prerequisites = data.GetProperty("waiting_for").EnumerateArray().Select(id => id.GetString()).ToArray();
            string label = phase is "done" or "blocked" ? Display(phase) : (active, pause, phase) switch
            {
                (true, true, _) => "Pausing",
                (_, true, _) => "Paused",
                (false, false, _) when prerequisites.Length > 0 => "Waiting for prerequisites",
                (false, false, _) when supervised => "Supervised recovery",
                (false, false, _) when queued => "Queued",
                (false, false, "queued") => "Queued",
                (false, false, _) => "Interrupted",
                _ => Display(phase),
            };
            string subtitle = $"{label}  ·  {step}/{count} accepted";
            if (state.TryGetProperty("retry_at", out var retryAt) && retryAt.ValueKind == JsonValueKind.String &&
                DateTimeOffset.TryParse(retryAt.GetString(), out var retryTime) && retryTime > DateTimeOffset.Now && !pause)
                subtitle = $"Retry at {retryTime.ToLocalTime():HH:mm:ss} · {step}/{count} accepted";
            // Every milestone of a pursuit can be accepted while its goal is still being judged, planned or missed.
            string current = step < count ? Get(data, "current_milestone") : !pursuit ? "All milestones accepted" : phase switch
            {
                "judging" => "Judging the finished build",
                "planning" => "Planning the next wave",
                "rescuing" => "Rescuing a stalled pursuit",
                "done" => "Goal met",
                "blocked" => "Goal not met yet",
                _ => "Milestones accepted",
            };
            if (prerequisites.Length > 0)
                current += $"\nWaiting for {string.Join(", ", prerequisites)}";
            string error = Get(state, "last_error");
            if (phase == "blocked" && error.Length > 0)
                current += $"\n{Brief(error)}";
            if (data.TryGetProperty("blocked_prerequisites", out var blocked) && blocked.GetArrayLength() > 0)
                current += "\nA prerequisite needs attention before this run can start.";
            if (state.TryGetProperty("provider_calls", out var calls))
                current += $"\n{calls} provider calls";
            if (phase == "blocked" && state.TryGetProperty("provider_issue", out var issue) &&
                issue.ValueKind == JsonValueKind.Object)
            {
                current += $"\n{Display(Get(issue, "role"))} · {Display(Get(issue, "provider"))}";
                if (issue.TryGetProperty("profile_dir", out var profile))
                    current += $"\n{profile.GetString()}";
            }
            string title = Get(spec, "title");
            if (spec.TryGetProperty("project", out var project))
                title = $"{project.GetString()} · {title}";
            return new(Get(data, "id"), title, phase, pause, active || supervised, queued, prerequisites.Length > 0,
                subtitle, current,
                ShortCommit(Get(state, "accepted")), Get(data, "result_branch"));
        }
    }

    private sealed record Activity(long Seq, string Time, string Label, string Detail)
    {
        public static Activity Parse(JsonElement data)
        {
            var details = data.GetProperty("details");
            string kind = Get(data, "kind");
            string Read(string key) => details.TryGetProperty(key, out var value) ? value.ToString() : "";
            int Count(string key) => details.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.Array
                ? value.GetArrayLength() : 0;
            bool Flag(string key) => details.TryGetProperty(key, out var value) && value.ValueKind == JsonValueKind.True;
            string Titles(string key, int take) => details.TryGetProperty(key, out var list) && list.ValueKind == JsonValueKind.Array
                ? string.Join("; ", list.EnumerateArray().Select(item => item.ValueKind == JsonValueKind.Object &&
                    item.TryGetProperty("title", out var title) ? title.ToString() : item.ToString()).Take(take)) : "";
            string Clock(string key) => DateTimeOffset.TryParse(Read(key), out var at)
                ? at.ToLocalTime().ToString(at.ToLocalTime().Date == DateTime.Today ? "HH:mm" : "MMM dd HH:mm") : "later";
            string Best() => Read("best_score").Length > 0 ? $", best {Read("best_score")}/100" : "";
            string label = kind switch
            {
                "goal_judged" => $"Goal judged {Read("score")}/100",
                "goal_met" => $"Goal met at {Read("score")}/100",
                "wave_completed" => $"Wave {Read("wave")} built; judging next",
                "wave_planned" => $"Wave {Read("wave")} planned: {Plural(Count("titles"), "milestone")}" +
                    (Count("conflicts") > 0 ? $" (planner flagged {Plural(Count("conflicts"), "conflict")})" : ""),
                "planner_started" => details.TryGetProperty("wave", out var judged) && judged.TryGetInt32(out int wave)
                    ? $"Planning wave {wave + 1}" : "Planning the next wave",
                "judge_started" => $"Judging wave {Read("wave")}",
                "judge_ledger_ready" => Count("failed") == 0 ? "Evidence ledger ready: all checks passed"
                    : $"Evidence ledger ready: {Plural(Count("failed"), "check")} failed",
                "judge_gate_finished" => $"Ledger check {Read("name")} " + Observed(details) switch
                    { true => "passed", false => "failed", null => "unscored" },
                "judge_invalid" => $"Judge returned no valid judgment (attempt {Read("attempt")} of 2)",
                "plan_invalid" => $"Planner returned no valid plan (attempt {Read("attempt")} of 2)",
                "pursuit_reverted" => $"Wave {Read("wave")} scored {Read("score")}/100; reverted to best build ({Read("best_score")}/100)",
                "pursuit_stopped" => $"Pursuit stopped: {StopReason(Read("reason"))}{Best()}",
                "pursuit_extended" => $"Pursuit extended by {Plural(details.TryGetProperty("waves", out var added) &&
                    added.TryGetInt32(out int count) ? count : 0, "wave")}" + (Flag("resumed") ? "; judging resumed" : ""),
                "judge_worktree_cleanup_failed" => "Judge workspace cleanup failed",
                "provider_parked" => Flag("parsed") ? $"Waiting for usage reset until {Clock("reset")}"
                    : $"Usage limit reached; retrying at {Clock("until")}",
                "worker_stalled" => "Worker stalled; retrying fresh",
                "review_unparseable" => "Reviewer answer unreadable; asking again",
                "independence_degraded" => $"{Display(Read("role"))} shares the worker's vendor",
                _ => Display(kind),
            };
            string detail = kind switch
            {
                "goal_judged" => Titles("gaps", 3) is { Length: > 0 } gaps ? $"Top gaps: {gaps}" : Brief(Read("reason")),
                "goal_met" => $"Build {ShortCommit(Read("commit"))} meets the goal",
                "wave_completed" => $"Verified commit {ShortCommit(Read("commit"))}",
                "wave_planned" => Titles("titles", 5) + (Count("conflicts") > 0 ? $"\nConflicts: {Titles("conflicts", 3)}" : ""),
                "planner_started" => $"{Display(Read("adapter"))} is planning from the judged gaps",
                "judge_started" => $"{Display(Read("adapter"))} is judging build {ShortCommit(Read("commit"))}",
                "judge_ledger_ready" => Count("failed") > 0 ? $"Failed: {Titles("failed", 5)}" : "Every check passed on the judged build",
                "judge_gate_finished" => $"{Read("name")} · Exit code {Read("exit")}",
                "judge_invalid" or "plan_invalid" or "judge_worktree_cleanup_failed" or "independence_degraded" => Brief(Read("issue")),
                "pursuit_reverted" => $"Discarded work kept at {Read("discarded_ref").Replace("refs/heads/", "")}",
                "pursuit_stopped" => (Read("best_commit").Length > 0
                    ? $"Best build {ShortCommit(Read("best_commit"))} stays on the result branch." : "No judged build to keep.") +
                    (Read("reason") == "milestone_limit" ? " More waves cannot lift the milestone limit."
                        : " Add waves with the pursuit command to continue."),
                "pursuit_extended" => details.TryGetProperty("max_waves", out var limit) && limit.TryGetInt32(out int most)
                    ? $"At most {most + 1} waves in total" : "",
                "provider_parked" => $"{Display(Read("role"))} · {Display(Read("provider"))}\n{Brief(Read("issue"))}",
                "worker_stalled" => $"Attempt {Read("attempt")} · stall {Read("stall")} of {Read("stalls_allowed")} · retry in {Read("seconds")} seconds",
                "review_unparseable" => $"Attempt {Read("attempt")} · unreadable answer {Read("count")} of 2",
                "queued" => $"Starting from {ShortCommit(Read("base"))}",
                "worktree_ready" => "Milestone workspace prepared",
                "worker_started" => $"{Display(Read("adapter"))} · Attempt {Read("attempt")}",
                "worker_finished" => $"Worker returned exit code {Read("exit")}",
                "gate_started" => Read("name"),
                "gate_finished" => $"{Read("name")} · Exit code {Read("exit")}",
                "checks_passed" => $"{details.GetProperty("checks").GetArrayLength()} checks passed",
                "review_started" => $"{Display(Read("adapter"))} is reviewing the result",
                "review_passed" => Read("reason"),
                "local_advice" => Read("note"),
                "local_advice_unavailable" => Read("reason"),
                "promotion_prepared" => $"Verified commit {ShortCommit(Read("commit"))}",
                "milestone_accepted" => $"Milestone {Read("step")} accepted · {ShortCommit(Read("commit"))}",
                "guard_child_started" => $"Runner {Read("pid")} · {Display(Read("phase"))}",
                "guard_child_exited" => $"Runner {Read("pid")} exited with {Read("exit")} · {ShortCommit(Read("accepted"))}",
                "guard_restart_scheduled" => $"Restart {Read("restart")} of {Read("limit")} · {Read("completed")} milestones accepted",
                "guard_stopped" => Display(Read("reason")),
                "run_completed" => Read("branch").Replace("refs/heads/", ""),
                "attempt_rejected" or "blocked" or "provider_unavailable" or "budget_exhausted" or "repeated_failure_stopped" => Brief(Read("issue")),
                "operator_feedback" => Brief(Read("message")),
                "provider_retry_scheduled" => $"Retry in {Read("seconds")} seconds · {Read("issue")}",
                "provider_fallback" => $"Switching provider route: {Read("to")}",
                "provider_call_finished" => $"{Read("role")} · {Read("seconds")}s · observed tokens: {Read("observed_tokens")}",
                _ => string.Join("\n", details.EnumerateObject()
                    .Where(property => property.Name is "reason" or "phase")
                    .Select(property => $"{Display(property.Name)}  {property.Value}")),
            };
            string time = DateTimeOffset.Parse(Get(data, "time")).ToLocalTime().ToString("HH:mm:ss");
            return new(data.GetProperty("seq").GetInt64(), time, label, detail);
        }
    }

    private static string Plural(int count, string noun) => $"{count} {noun}{(count == 1 ? "" : "s")}";

    private static string StopReason(string reason) => reason switch
    {
        "wave_limit" => "wave limit reached",
        "plateau" => "score plateau",
        "milestone_limit" => "milestone limit reached",
        _ => reason.Replace('_', ' '),
    };

    // Mirrors checks.observed_pass: only a completed, evidenced check scores.
    private static bool? Observed(JsonElement receipt)
    {
        bool Set(string key) => receipt.TryGetProperty(key, out var value) &&
            value.ValueKind is not (JsonValueKind.Null or JsonValueKind.False) &&
            !(value.ValueKind == JsonValueKind.String && value.GetString()!.Length == 0);
        if (!receipt.TryGetProperty("exit", out var exit) || !exit.TryGetInt32(out int code) || Set("artifact_error") || Set("source_error"))
            return null;
        bool junit = !(receipt.TryGetProperty("junit", out var report) && report.ValueKind == JsonValueKind.Object &&
            report.TryGetProperty("passed", out var passed) && passed.ValueKind == JsonValueKind.False);
        return code == 0 && junit;
    }

    private static string Brief(string value)
    {
        if (value.Contains("Failed to authenticate", StringComparison.OrdinalIgnoreCase))
            return "The selected provider login has expired. Sign in to that CLI profile and retry.";
        return value.Length <= 380 ? value : value[..380] + "… Open the evidence folder for the full log.";
    }

    private static string Get(JsonElement data, string key) => data.GetProperty(key).GetString() ?? "";
    private static string ShortCommit(string hash) => hash.Length == 0 ? "unavailable" : hash[..Math.Min(12, hash.Length)];
    private static string Display(string value) => value.Length == 0 ? value : char.ToUpperInvariant(value[0]) + value[1..].Replace('_', ' ');
}
