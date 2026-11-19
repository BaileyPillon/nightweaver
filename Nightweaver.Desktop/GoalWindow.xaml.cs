using Microsoft.Win32;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;

namespace Nightweaver.Desktop;

public partial class GoalWindow : Window
{
    private readonly Func<string, Task<JsonElement>> preview;
    private string? baseline;
    private int revision;
    private bool RepositoryMode => RepoMode.IsChecked == true;
    private string Provider => (string)((ComboBoxItem)Planner.SelectedItem).Tag;
    private string Alternate => (string)((ComboBoxItem)Rescue.SelectedItem).Tag;
    public string[] Arguments()
    {
        var args = new List<string> { RepositoryMode ? "oneshot" : "goal", SourcePath.Text, GoalText.Text.Trim(),
            RepositoryMode ? "--worker" : "--planner", Provider };
        if (RepositoryMode)
        {
            args.AddRange(["--expected-base", baseline!]);
            if (Alternate.Length > 0 && Alternate != Provider) args.AddRange(["--rescue", Alternate]);
        }
        else args.Add("--start");
        if (ProfilePath.Text.Trim() is { Length: > 0 } profile) args.AddRange(["--profile-dir", profile]);
        return args.ToArray();
    }

    public GoalWindow(Func<string, Task<JsonElement>> preview)
    {
        this.preview = preview;
        InitializeComponent();
        ResetSource();
    }

    private void ModeChanged(object sender, RoutedEventArgs e)
    {
        if (IsInitialized) ResetSource();
    }

    private void ResetSource()
    {
        revision++;
        baseline = null;
        SourcePath.Clear();
        Checks.Text = RepositoryMode ? "Choose a repository to see its committed checks." : "Choose a Nightweaver JSON template.";
        ProviderLabel.Content = RepositoryMode ? "Planner and worker" : "Planner";
        RescuePanel.Visibility = RepositoryMode ? Visibility.Visible : Visibility.Collapsed;
        RefreshButton();
    }

    private async void Browse_Click(object sender, RoutedEventArgs e)
    {
        if (!RepositoryMode)
        {
            var picker = new OpenFileDialog {Title="Select a OneShot template", Filter="Nightweaver plan (*.json)|*.json"};
            if (picker.ShowDialog(this) == true) { SourcePath.Text = picker.FileName; GoalText.Focus(); RefreshButton(); }
            return;
        }
        var folder = new OpenFolderDialog {Title="Choose a Git repository"};
        if (folder.ShowDialog(this) == true) await PreviewRepository(folder.FolderName);
    }

    internal async Task PreviewRepository(string path)
    {
        int request = ++revision;
        SourcePath.Text = path;
        baseline = null;
        Checks.Text = "Finding committed checks…";
        RefreshButton();
        try
        {
            var plan = await preview(path);
            if (request != revision) return;
            baseline = plan.GetProperty("expected_base").GetString();
            var names = plan.GetProperty("gates").EnumerateArray().Select(g => g.GetProperty("name").GetString()).ToArray();
            Checks.Text = $"{names.Length} {(names.Length == 1 ? "check" : "checks")} at {baseline![..8]} · {string.Join(", ", names.Take(5))}" +
                          (names.Length > 5 ? $" (+{names.Length - 5} more)" : "");
            GoalText.Focus();
        }
        catch (Exception error)
        {
            if (request == revision) Checks.Text = error.Message;
        }
        RefreshButton();
    }

    private void InputsChanged(object sender, RoutedEventArgs e)
    {
        if (!IsInitialized) return;
        if (RepositoryMode && Alternate == Provider) Rescue.SelectedIndex = 0;
        RefreshButton();
    }

    private void RefreshButton()
    {
        StartButton.IsEnabled = !string.IsNullOrWhiteSpace(GoalText.Text) && SourcePath.Text.Length > 0
            && (!RepositoryMode || baseline != null);
    }

    private void Start_Click(object sender, RoutedEventArgs e) => DialogResult = true;
}
