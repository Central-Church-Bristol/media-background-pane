using System;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

[assembly: AssemblyTitle("Media Background Pane")]
[assembly: AssemblyProduct("Media Background Pane")]
[assembly: AssemblyDescription("Media Background Pane")]

namespace MediaBackgroundPane
{
    // Starts Media Background Pane (main.py with the folder's own Python, .venv) without a console window. Python runs
    // inside this program, so Windows sees Media Background Pane, not Python: in Task Manager and on the tray's
    // notifications. If that can't be done, it starts the venv's pythonw.exe instead, saying why (the app writes it in
    // its log). It has to stay in the Media Background Pane folder, next to main.py: put a shortcut to it in the Start
    // menu instead of moving it. Start with Windows runs it too (startup.py).
    // (CentraLight's launcher, 8 Nov 2026, with the names changed; before 18 Nov the pane ran in pythonw.exe.)
    static class Program
    {
        [STAThread]
        static int Main(string[] args)
        {
            string dir = Path.GetDirectoryName(Assembly.GetExecutingAssembly().Location);
            string pythonw = Path.Combine(dir, ".venv", "Scripts", "pythonw.exe");
            string script = Path.Combine(dir, "main.py");

            if (!File.Exists(pythonw) || !File.Exists(script))
            {
                string setup = Path.Combine(dir, "run.bat");
                if (File.Exists(setup))
                {
                    // the first time: run.bat sets everything up, then starts the app
                    Process.Start(new ProcessStartInfo
                    {
                        FileName = setup,
                        WorkingDirectory = dir,
                        UseShellExecute = true,
                    });
                    return 0;
                }

                MessageBox.Show(
                    "Media Background Pane.exe has to stay in the Media Background Pane folder, next to main.py.\n\n" +
                    "Put a shortcut to it in the Start menu instead of moving it.",
                    "Media Background Pane",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
                return 1;
            }

            Directory.SetCurrentDirectory(dir);
            int code;
            string problem;
            if (Inside.TryRun(dir, pythonw, script, args, out code, out problem))
                return code;
            return StartSeparately(dir, pythonw, script, args, problem);
        }

        // The venv's pythonw.exe in a process of its own.
        static int StartSeparately(string dir, string pythonw, string script, string[] args, string problem)
        {
            var start = new ProcessStartInfo();
            start.FileName = pythonw;
            start.WorkingDirectory = dir;
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.Arguments = Quote(script);
            foreach (string arg in args)
                start.Arguments += " " + Quote(arg);
            start.EnvironmentVariables["MEDIA_BACKGROUND_PANE_SEPARATE"] = problem;

            try
            {
                Process.Start(start);
                return 0;
            }
            catch (Exception ex)
            {
                MessageBox.Show(
                    "Media Background Pane couldn't start:\n\n" + ex.Message,
                    "Media Background Pane",
                    MessageBoxButtons.OK,
                    MessageBoxIcon.Error);
                return 1;
            }
        }

        static string Quote(string value)
        {
            if (value.Length > 0 && value.IndexOfAny(new[] { ' ', '\t', '"' }) < 0)
                return value;
            var text = new StringBuilder("\"");
            int backslashes = 0;
            foreach (char c in value)
            {
                if (c == '\\')
                {
                    backslashes++;
                    continue;
                }
                if (c == '"')
                {
                    text.Append('\\', backslashes * 2 + 1);
                    text.Append('"');
                }
                else
                {
                    text.Append('\\', backslashes);
                    text.Append(c);
                }
                backslashes = 0;
            }
            text.Append('\\', backslashes * 2);
            text.Append('"');
            return text.ToString();
        }
    }

    // Python inside this program, with the Python Configuration API of Python 3.14 (PEP 741): the venv's own Python
    // (pyvenv.cfg's home), told it's the venv's pythonw.exe, so it finds the venv and its packages as pythonw.exe would,
    // and runs main.py as pythonw.exe main.py would.
    static class Inside
    {
        [DllImport("kernel32", CharSet = CharSet.Unicode, SetLastError = true)]
        static extern IntPtr LoadLibraryExW(string path, IntPtr file, uint flags);

        [DllImport("kernel32", CharSet = CharSet.Ansi, SetLastError = true, ExactSpelling = true)]
        static extern IntPtr GetProcAddress(IntPtr module, string name);

        const uint LOAD_WITH_ALTERED_SEARCH_PATH = 0x8;  // (its DLLs from its own folder: vcruntime140.dll)

        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate IntPtr CreateFn();
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate void FreeFn(IntPtr config);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int SetStrFn(IntPtr config, byte[] name, byte[] value);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int SetIntFn(IntPtr config, byte[] name, long value);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int SetStrListFn(IntPtr config, byte[] name, UIntPtr length, IntPtr[] items);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int InitFn(IntPtr config);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int GetErrorFn(IntPtr config, out IntPtr message);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int GetExitCodeFn(IntPtr config, out int exitcode);
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)] delegate int RunMainFn();

        // True once Python has run (code: its exit code), or false with what stopped it before it started.
        public static bool TryRun(string dir, string pythonw, string script, string[] args, out int code, out string problem)
        {
            code = 0;
            problem = "";
            string home = Home(Path.Combine(dir, ".venv", "pyvenv.cfg"));
            if (home == null)
            {
                problem = ".venv\\pyvenv.cfg has no home";
                return false;
            }
            string dll = PythonDll(home);
            if (dll == null)
            {
                problem = "no python3NN.dll in " + home;
                return false;
            }
            IntPtr module = LoadLibraryExW(dll, IntPtr.Zero, LOAD_WITH_ALTERED_SEARCH_PATH);
            if (module == IntPtr.Zero)
            {
                problem = "couldn't load " + dll + " (Windows error " + Marshal.GetLastWin32Error() + ")";
                return false;
            }

            CreateFn create; FreeFn free; SetStrFn setStr; SetIntFn setInt; SetStrListFn setStrList; InitFn init;
            GetErrorFn getError; GetExitCodeFn getExitCode; RunMainFn runMain;
            if (!Find(module, "PyInitConfig_Create", out create) || !Find(module, "PyInitConfig_Free", out free)
                || !Find(module, "PyInitConfig_SetStr", out setStr) || !Find(module, "PyInitConfig_SetInt", out setInt)
                || !Find(module, "PyInitConfig_SetStrList", out setStrList)
                || !Find(module, "Py_InitializeFromInitConfig", out init)
                || !Find(module, "PyInitConfig_GetError", out getError)
                || !Find(module, "PyInitConfig_GetExitCode", out getExitCode) || !Find(module, "Py_RunMain", out runMain))
            {
                problem = Path.GetFileName(dll) + " has no Python Configuration API (it needs Python 3.14 or later)";
                return false;
            }

            IntPtr config = create();
            if (config == IntPtr.Zero)
            {
                problem = "PyInitConfig_Create gave nothing";
                return false;
            }
            var argv = new string[args.Length + 1];
            argv[0] = script;
            Array.Copy(args, 0, argv, 1, args.Length);
            IntPtr[] items = new IntPtr[argv.Length];
            try
            {
                for (int i = 0; i < argv.Length; i++)
                    items[i] = Utf8Alloc(argv[i]);
                bool ready =
                    setStr(config, Utf8("executable"), Utf8(pythonw)) == 0      // (so it finds the venv: pyvenv.cfg)
                    && setStr(config, Utf8("run_filename"), Utf8(script)) == 0
                    && setStrList(config, Utf8("argv"), (UIntPtr)items.Length, items) == 0
                    && setInt(config, Utf8("safe_path"), 0) == 0                 // (main.py's folder first on sys.path)
                    && setInt(config, Utf8("isolated"), 0) == 0
                    && setInt(config, Utf8("use_environment"), 1) == 0           // (as pythonw.exe: PYTHON* settings count)
                    && setInt(config, Utf8("user_site_directory"), 0) == 0
                    && setInt(config, Utf8("install_signal_handlers"), 1) == 0;
                if (!ready || init(config) < 0)
                {
                    int exitcode;
                    if (getExitCode(config, out exitcode) == 1)  // (Python asked to exit: done)
                    {
                        code = exitcode;
                        return true;
                    }
                    problem = Error(getError, config);
                    return false;
                }
            }
            finally
            {
                free(config);
                foreach (IntPtr item in items)
                    if (item != IntPtr.Zero)
                        Marshal.FreeHGlobal(item);
            }
            code = runMain();
            return true;
        }

        // pyvenv.cfg's "home = C:\...\pythoncore-3.14-64": the Python the venv was made from.
        static string Home(string cfg)
        {
            if (!File.Exists(cfg))
                return null;
            foreach (string line in File.ReadAllLines(cfg))
            {
                int equals = line.IndexOf('=');
                if (equals > 0 && line.Substring(0, equals).Trim().ToLowerInvariant() == "home")
                {
                    string home = line.Substring(equals + 1).Trim();
                    return Directory.Exists(home) ? home : null;
                }
            }
            return null;
        }

        // Its python3NN.dll (python314.dll), not python3.dll.
        static string PythonDll(string home)
        {
            foreach (string path in Directory.GetFiles(home, "python3*.dll"))
            {
                string name = Path.GetFileNameWithoutExtension(path).ToLowerInvariant();
                if (name.Length > "python3".Length && char.IsDigit(name[name.Length - 1]))
                    return path;
            }
            return null;
        }

        static bool Find<T>(IntPtr module, string name, out T function) where T : class
        {
            IntPtr address = GetProcAddress(module, name);
            function = address == IntPtr.Zero ? null : Marshal.GetDelegateForFunctionPointer(address, typeof(T)) as T;
            return function != null;
        }

        static string Error(GetErrorFn getError, IntPtr config)
        {
            IntPtr message;
            if (getError(config, out message) == 1 && message != IntPtr.Zero)
                return "Python said: " + FromUtf8(message);
            return "Python wouldn't start";
        }

        static byte[] Utf8(string text)
        {
            byte[] bytes = Encoding.UTF8.GetBytes(text);
            Array.Resize(ref bytes, bytes.Length + 1);  // (ends with a 0)
            return bytes;
        }

        static IntPtr Utf8Alloc(string text)
        {
            byte[] bytes = Utf8(text);
            IntPtr memory = Marshal.AllocHGlobal(bytes.Length);
            Marshal.Copy(bytes, 0, memory, bytes.Length);
            return memory;
        }

        static string FromUtf8(IntPtr text)
        {
            int length = 0;
            while (Marshal.ReadByte(text, length) != 0)
                length++;
            byte[] bytes = new byte[length];
            Marshal.Copy(text, bytes, 0, length);
            return Encoding.UTF8.GetString(bytes);
        }
    }
}
