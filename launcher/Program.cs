using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("Media Background Pane")]
[assembly: AssemblyProduct("Media Background Pane")]
[assembly: AssemblyDescription("Media Background Pane")]

namespace MediaBackgroundPane
{
    static class Program
    {
        [STAThread]
        static void Main(string[] args)
        {
            string dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            string pythonw = Path.Combine(dir, ".venv", "Scripts", "pythonw.exe");
            string script = Path.Combine(dir, "main.py");

            if (!File.Exists(pythonw) || !File.Exists(script))
            {
                string setup = Path.Combine(dir, "run.bat");
                if (File.Exists(setup))
                {
                    Process.Start(new ProcessStartInfo
                    {
                        FileName = setup,
                        WorkingDirectory = dir,
                        UseShellExecute = true,
                    });
                    return;
                }

                MessageBox.Show(
                    "Could not find the Media Background Pane app next to this program.",
                    "Media Background Pane",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
                return;
            }

            var start = new ProcessStartInfo();
            start.FileName = pythonw;
            start.WorkingDirectory = dir;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.Arguments = Quote(script);
            foreach (string arg in args)
                start.Arguments += " " + Quote(arg);

            try
            {
                Process.Start(start);
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    ex.Message,
                    "Media Background Pane",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
            }
        }

        static string Quote(string value)
        {
            if (string.IsNullOrEmpty(value))
                return "\"\"";
            if (value.IndexOfAny(new[] { ' ', '\t', '\n', '\v', '"' }) < 0)
                return value;

            var sb = new StringBuilder();
            sb.Append('"');
            int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\')
                {
                    slashes++;
                    continue;
                }
                if (c == '"')
                {
                    sb.Append('\\', slashes * 2 + 1);
                    sb.Append('"');
                    slashes = 0;
                    continue;
                }
                if (slashes > 0)
                {
                    sb.Append('\\', slashes);
                    slashes = 0;
                }
                sb.Append(c);
            }
            if (slashes > 0)
                sb.Append('\\', slashes * 2);
            sb.Append('"');
            return sb.ToString();
        }
    }
}
