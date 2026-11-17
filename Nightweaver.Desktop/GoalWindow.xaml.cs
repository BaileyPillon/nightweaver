using System.Windows;
using System.Windows.Controls;

namespace Nightweaver.Desktop;

public partial class GoalWindow : Window
{
    public string Objective => GoalText.Text.Trim();
    public string Provider => (string)((ComboBoxItem)Planner.SelectedItem).Tag;

    public GoalWindow()
    {
        InitializeComponent();
        Loaded += (_, _) => GoalText.Focus();
    }

    private void Start_Click(object sender, RoutedEventArgs e)
    {
        if (Objective.Length > 0)
            DialogResult = true;
        else
            GoalText.Focus();
    }
}
