using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

internal static class Launcher {
    [STAThread]
    static int Main(string[] args) {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        bool silent = Array.IndexOf(args, "--silent") >= 0;
        bool shutdown = Array.IndexOf(args, "--shutdown") >= 0;
        try {
            var start = new ProcessStartInfo(Path.Combine(root, "runtime", "python.exe"));
            start.Arguments = "-m nanobot.desktop" + (silent ? " --silent" : "") + (shutdown ? " --shutdown" : "");
            start.WorkingDirectory = root;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.RedirectStandardOutput = true;
            start.RedirectStandardError = true;
            start.EnvironmentVariables.Remove("PYTHONPATH");
            start.EnvironmentVariables.Remove("PYTHONHOME");
            start.EnvironmentVariables["PYTHONUTF8"] = "1";
            using (var child = Process.Start(start)) {
                child.OutputDataReceived += (s, e) => { };
                child.ErrorDataReceived += (s, e) => { };
                child.BeginOutputReadLine();
                child.BeginErrorReadLine();
                child.WaitForExit();
                return child.ExitCode;
            }
        } catch {
            if (!silent && !shutdown) MessageBox.Show("无法启动应用，请重新安装 CatgirlMentor。用户数据将会保留。", "CatgirlMentor", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
}
