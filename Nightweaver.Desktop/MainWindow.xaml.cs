using System.Diagnostics;
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
    private bool refreshing;
    private bool updatingList;
    private bool working;

    public MainWindow()
    {
        InitializeComponent();
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
            string? selected = select ?? (RunList.SelectedItem as RunRow)?.Id;
            var runs = (await CallAsync("list")).EnumerateArray().Select(RunRow.Parse).ToArray();
            updatingList = true;
            try
            {
                if (RunList.ItemsSource is not RunRow[] old || !old.SequenceEqual(runs))
                    RunList.ItemsSource = runs;
                RunList.SelectedItem = ((RunRow[])RunList.ItemsSource).FirstOrDefault(row => row.Id == selected)
                    ?? ((RunRow[])RunList.ItemsSource).FirstOrDefault();
            }
            finally
            {
                updatingList = false;
            }
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

    private async Task RenderSelectionAsync()
    {
        if (RunList.SelectedItem is not RunRow row)
            return;
        RunTitle.Text = row.Title;
        RunSummary.Text = $"{row.Subtitle}\n{row.Current}".TrimEnd();
        Branch.Text = $"{row.ResultBranch}    {row.Accepted}";
        Branch.Visibility = Visibility.Visible;
        EvidenceButton.IsEnabled = true;
        PauseButton.IsEnabled = !working && row.Active && !row.Pause && row.Phase is not ("done" or "blocked");
        ResumeButton.IsEnabled = !working && !row.Active && row.Phase != "done";
        ResumeButton.Content = row.Phase == "blocked" ? "Retry" : "Resume";

        var events = (await CallAsync("events", row.Id)).EnumerateArray().Select(Activity.Parse).ToArray();
        if ((RunList.SelectedItem as RunRow)?.Id != row.Id)
            return;
        long seq = events.LastOrDefault()?.Seq ?? 0;
        if (visibleRun == row.Id && lastEvent == seq)
            return;
        visibleRun = row.Id;
        lastEvent = seq;
        Timeline.ItemsSource = events;
        if (events.Length != 0)
            Timeline.ScrollIntoView(events[^1]);
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
        if (working || PickPlan("Select a OneShot template") is not string path)
            return;
        var dialog = new GoalWindow { Owner = this };
        if (dialog.ShowDialog() == true)
            await PerformAsync("Planning milestones…", "goal", path, dialog.Objective, "--planner", dialog.Provider, "--start");
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

    private void Evidence_Click(object sender, RoutedEventArgs e)
    {
        if (RunList.SelectedItem is not RunRow row)
            return;
        try { Process.Start(new ProcessStartInfo(Path.Combine(dataRoot, "runs", row.Id)) { UseShellExecute = true }); }
        catch (Exception error) { Status.Text = error.Message; }
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

    private sealed record RunRow(string Id, string Title, string Phase, bool Pause, bool Active, string Subtitle, string Current, string Accepted, string ResultBranch)
    {
        public static RunRow Parse(JsonElement data)
        {
            var spec = data.GetProperty("spec");
            var state = data.GetProperty("state");
            var milestones = spec.GetProperty("milestones");
            int step = state.GetProperty("step").GetInt32();
            int count = milestones.GetArrayLength();
            string phase = Get(state, "phase");
            bool pause = data.GetProperty("pause").GetBoolean();
            bool active = data.GetProperty("runner_active").GetBoolean();
            string label = phase is "done" or "blocked" ? Display(phase) : (active, pause, phase) switch
            {
                (true, true, _) => "Pausing",
                (_, true, _) => "Paused",
                (false, false, "queued") => "Queued",
                (false, false, _) => "Interrupted",
                _ => Display(phase),
            };
            string subtitle = $"{label}  ·  {step}/{count} accepted";
            string current = step < count ? Get(milestones[step], "title") : "All milestones accepted";
            string error = Get(state, "last_error");
            if (phase == "blocked" && error.Length > 0)
                current += $"\n{Brief(error)}";
            return new(Get(data, "id"), Get(spec, "title"), phase, pause, active, subtitle, current,
                Get(state, "accepted")[..12], Get(data, "result_branch"));
        }
    }

    private sealed record Activity(long Seq, string Time, string Label, string Detail)
    {
        public static Activity Parse(JsonElement data)
        {
            var details = data.GetProperty("details");
            string kind = Get(data, "kind");
            string Read(string key) => details.TryGetProperty(key, out var value) ? value.ToString() : "";
            string detail = kind switch
            {
                "queued" => $"Starting from {Read("base")[..12]}",
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
                "promotion_prepared" => $"Verified commit {Read("commit")[..12]}",
                "milestone_accepted" => $"Milestone {Read("step")} accepted · {Read("commit")[..12]}",
                "run_completed" => Read("branch").Replace("refs/heads/", ""),
                "attempt_rejected" or "blocked" or "provider_unavailable" => Brief(Read("issue")),
                _ => string.Join("\n", details.EnumerateObject()
                    .Where(property => property.Name is "reason" or "phase")
                    .Select(property => $"{Display(property.Name)}  {property.Value}")),
            };
            string time = DateTimeOffset.Parse(Get(data, "time")).ToLocalTime().ToString("HH:mm:ss");
            return new(data.GetProperty("seq").GetInt64(), time, Display(kind), detail);
        }
    }

    private static string Brief(string value)
    {
        if (value.Contains("Failed to authenticate", StringComparison.OrdinalIgnoreCase))
            return "The selected provider login has expired. Sign in to that CLI profile and retry.";
        return value.Length <= 380 ? value : value[..380] + "… Open the evidence folder for the full log.";
    }

    private static string Get(JsonElement data, string key) => data.GetProperty(key).GetString() ?? "";
    private static string Display(string value) => value.Length == 0 ? value : char.ToUpperInvariant(value[0]) + value[1..].Replace('_', ' ');
}
