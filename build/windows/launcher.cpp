// Windows 7 or later: GetCurrentConsoleFontEx and the virtual-terminal
// console flags are declared behind this.
#ifndef _WIN32_WINNT
#define _WIN32_WINNT 0x0601
#endif

#include <windows.h>
#include <wslapi.h>

#include <cstdio>
#include <cwchar>
#include <string>

// Older mingw headers declare the console handles but not every mode bit that
// Windows 10 added. Defining the missing ones is what the SDK would have.
#ifndef ENABLE_VIRTUAL_TERMINAL_PROCESSING
#define ENABLE_VIRTUAL_TERMINAL_PROCESSING 0x0004
#endif
#ifndef DISABLE_NEWLINE_AUTO_RETURN
#define DISABLE_NEWLINE_AUTO_RETURN 0x0008
#endif
#ifndef ENABLE_VIRTUAL_TERMINAL_INPUT
#define ENABLE_VIRTUAL_TERMINAL_INPUT 0x0200
#endif
#ifndef ENABLE_QUICK_EDIT_MODE
#define ENABLE_QUICK_EDIT_MODE 0x0040
#endif
#ifndef ENABLE_EXTENDED_FLAGS
#define ENABLE_EXTENDED_FLAGS 0x0080
#endif
#ifndef ENABLE_MOUSE_INPUT
#define ENABLE_MOUSE_INPUT 0x0010
#endif
#ifndef TMPF_TRUETYPE
#define TMPF_TRUETYPE 0x04
#endif

namespace {

constexpr wchar_t kDefaultDistribution[] = L"Laintas-CLI";
constexpr wchar_t kLinuxExecutable[] = L"/usr/local/bin/laintas-cli";
constexpr wchar_t kTerminalProfile[] = L"Laintas CLI";
// Bundled Windows Terminal, relative to the directory holding this launcher.
constexpr wchar_t kBundledTerminal[] = L"..\\terminal\\WindowsTerminal.exe";

// The console fixes below are all conditional and all reversible. A launcher
// that refuses to start because it could not set a font would be worse than
// the ugly console it was trying to improve, so every step here is allowed to
// fail quietly and the launch continues.

std::wstring ReadEnvironment(const wchar_t* name) {
    DWORD required = GetEnvironmentVariableW(name, nullptr, 0);
    if (required == 0) {
        return std::wstring();
    }
    std::wstring value(required, L'\0');
    DWORD written = GetEnvironmentVariableW(name, value.data(), required);
    if (written == 0 || written >= required) {
        return std::wstring();
    }
    value.resize(written);
    return value;
}

std::wstring ReadDistributionName() {
    const std::wstring value = ReadEnvironment(L"LAINTAS_WSL_DISTRO");
    return value.empty() ? std::wstring(kDefaultDistribution) : value;
}

// WslLaunchInteractive accepts one Linux shell command rather than an argv
// array. Quote every Windows argument as one POSIX shell word. A literal
// single quote inside a single-quoted word is represented by: '\''
std::wstring PosixQuote(const std::wstring& value) {
    if (value.empty()) {
        return L"''";
    }
    std::wstring quoted = L"'";
    for (wchar_t ch : value) {
        if (ch == L'\'') {
            quoted += L"'\\''";
        } else {
            quoted.push_back(ch);
        }
    }
    quoted.push_back(L'\'');
    return quoted;
}

// ── the console we were given ────────────────────────────────────────────
//
// A shortcut or the installer's finish page starts this program in whatever
// terminal Windows picks. On Windows 10 that is conhost, and these settings
// are the most that can be salvaged there: virtual-terminal processing so
// escape sequences are not printed as literal text, UTF-8 code pages so
// non-ASCII output is not mojibake, and a TrueType font so a drawn frame is
// not a row of hollow boxes.
//
// What they cannot salvage is the mouse. conhost delivers mouse input only
// as MOUSE_EVENT INPUT_RECORDs and never as VT sequences, even with
// ENABLE_VIRTUAL_TERMINAL_INPUT set — Microsoft states this is deliberate
// (microsoft/terminal#15296). A Linux process reading bytes from a pty can
// only ever see VT sequences, so a WSL application in conhost cannot receive
// a click no matter what this function sets. QuickEdit is still cleared,
// because leaving it on adds accidental output-freezing selection on top of
// a mouse that does not work; but clearing it buys nothing else.
//
// That limit is why the terminal chain below exists: the fix for the mouse
// is to not be in conhost, and Windows 10 does not ship a terminal that can
// deliver one.

struct ConsoleState {
    HANDLE input = INVALID_HANDLE_VALUE;
    HANDLE output = INVALID_HANDLE_VALUE;
    DWORD input_mode = 0;
    DWORD output_mode = 0;
    UINT input_cp = 0;
    UINT output_cp = 0;
    bool restore_input = false;
    bool restore_output = false;
    bool restore_code_pages = false;
};

void SetFaceName(CONSOLE_FONT_INFOEX& font, const wchar_t* face) {
    std::wcsncpy(font.FaceName, face, LF_FACESIZE - 1);
    font.FaceName[LF_FACESIZE - 1] = L'\0';
}

void UseTrueTypeFont(HANDLE output) {
    // Only conhost honours this; Windows Terminal ignores it and keeps its
    // own font, which is already a TrueType one.
    CONSOLE_FONT_INFOEX font;
    ZeroMemory(&font, sizeof(font));
    font.cbSize = sizeof(font);
    if (!GetCurrentConsoleFontEx(output, FALSE, &font)) {
        return;
    }
    // TMPF_TRUETYPE is the only reliable way to tell the two apart. The
    // legacy raster font is not "family 0 with an empty face name" — conhost
    // reports it as face "Terminal", family 48 (FF_MODERN) — so a guard
    // written that way returns early on exactly the console it was meant to
    // repair, and the box-drawing and CJK glyphs stay broken. Any TrueType
    // face is a deliberate choice and is left alone; a raster face can never
    // draw a frame, whoever selected it.
    if (font.FontFamily & TMPF_TRUETYPE) {
        return;
    }
    font.FontFamily = FF_DONTCARE;
    font.FontWeight = FW_NORMAL;
    font.dwFontSize.X = 0;
    font.dwFontSize.Y = 18;
    // Cascadia Mono ships with Windows Terminal and recent Windows 10/11;
    // Consolas is on every Windows since Vista. Try the better one first.
    SetFaceName(font, L"Cascadia Mono");
    if (SetCurrentConsoleFontEx(output, FALSE, &font)) {
        return;
    }
    SetFaceName(font, L"Consolas");
    SetCurrentConsoleFontEx(output, FALSE, &font);
}

ConsoleState PrepareConsole() {
    ConsoleState state;
    state.input = GetStdHandle(STD_INPUT_HANDLE);
    state.output = GetStdHandle(STD_OUTPUT_HANDLE);

    state.input_cp = GetConsoleCP();
    state.output_cp = GetConsoleOutputCP();
    if (state.input_cp != 0 && state.output_cp != 0) {
        // Without this the console decodes output as the system code page —
        // 936 on a Chinese install — and every non-ASCII byte the CLI writes
        // comes out as mojibake.
        // Two independent settings: `&&` would skip the output code page
        // whenever the input one failed, and record nothing to restore for
        // the half of the change that did land.
        const bool input_utf8 = SetConsoleCP(CP_UTF8) != FALSE;
        const bool output_utf8 = SetConsoleOutputCP(CP_UTF8) != FALSE;
        state.restore_code_pages = input_utf8 || output_utf8;
    }

    if (state.output != INVALID_HANDLE_VALUE
            && GetConsoleMode(state.output, &state.output_mode)) {
        DWORD mode = state.output_mode
            | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            | DISABLE_NEWLINE_AUTO_RETURN;
        state.restore_output = SetConsoleMode(state.output, mode) != FALSE;
        UseTrueTypeFont(state.output);
    }

    if (state.input != INVALID_HANDLE_VALUE
            && GetConsoleMode(state.input, &state.input_mode)) {
        DWORD mode = state.input_mode;
        // ENABLE_EXTENDED_FLAGS is required for the QuickEdit bit to be read
        // at all: without it SetConsoleMode ignores the change and the mouse
        // stays captured by the console's own selection.
        mode |= ENABLE_EXTENDED_FLAGS | ENABLE_MOUSE_INPUT
              | ENABLE_VIRTUAL_TERMINAL_INPUT;
        mode &= ~static_cast<DWORD>(ENABLE_QUICK_EDIT_MODE);
        state.restore_input = SetConsoleMode(state.input, mode) != FALSE;
    }
    return state;
}

// Mouse reporting, bracketed paste and cursor visibility are held by the
// terminal, not by any process, so a Laintas run that died without cleaning
// up leaves the terminal reporting into the next one. The CLI clears them
// when it starts, but that is several seconds away: the distribution has to
// boot and Python has to import first, and every mouse movement in that
// window is echoed as ``^[[<35;46;1M``. Clearing them here closes the window
// entirely — this is the first thing that runs after the terminal opens.
void ClearInheritedTerminalModes(HANDLE output) {
    if (output == INVALID_HANDLE_VALUE) {
        return;
    }
    // Deliberately not the alternate screen: leaving it is the terminal's
    // business, and a stray 1049l here would discard scrollback the user
    // can still see.
    static const char kReset[] =
        "\x1b[?1000l"      // normal mouse tracking
        "\x1b[?1002l"      // button-event tracking
        "\x1b[?1003l"      // any-motion tracking: the flood
        "\x1b[?1015l"      // urxvt extended coordinates
        "\x1b[?1006l"      // SGR extended coordinates
        "\x1b[?2004l"      // bracketed paste
        "\x1b[?25h";       // cursor visible
    DWORD written = 0;
    WriteFile(output, kReset, sizeof(kReset) - 1, &written, nullptr);
}

void RestoreConsole(const ConsoleState& state) {
    if (state.restore_input) {
        SetConsoleMode(state.input, state.input_mode);
    }
    if (state.restore_output) {
        SetConsoleMode(state.output, state.output_mode);
    }
    if (state.restore_code_pages) {
        SetConsoleCP(state.input_cp);
        SetConsoleOutputCP(state.output_cp);
    }
}

// ── Windows Terminal ─────────────────────────────────────────────────────

bool TerminalProfileInstalled() {
    const std::wstring local = ReadEnvironment(L"LOCALAPPDATA");
    if (local.empty()) {
        return false;
    }
    const std::wstring fragment = local
        + L"\\Microsoft\\Windows Terminal\\Fragments\\Laintas.LaintasCLI"
          L"\\laintas-cli.json";
    const DWORD attributes = GetFileAttributesW(fragment.c_str());
    return attributes != INVALID_FILE_ATTRIBUTES
        && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

std::wstring LauncherDirectory() {
    wchar_t path[MAX_PATH];
    const DWORD written = GetModuleFileNameW(nullptr, path, MAX_PATH);
    if (written == 0 || written >= MAX_PATH) {
        return std::wstring();
    }
    std::wstring full(path, written);
    const size_t slash = full.find_last_of(L'\\');
    return slash == std::wstring::npos ? std::wstring() : full.substr(0, slash);
}

bool FileExists(const std::wstring& path) {
    const DWORD attributes = GetFileAttributesW(path.c_str());
    return attributes != INVALID_FILE_ATTRIBUTES
        && !(attributes & FILE_ATTRIBUTE_DIRECTORY);
}

// The bundled Terminal is unpackaged, so unlike the Store build it does not
// carry the Visual C++ runtime with it. Asking the loader is the direct test;
// inferring it from a registry key guesses at the same question.
bool VisualCppRuntimePresent() {
    const HMODULE loaded = LoadLibraryExW(L"vcruntime140_1.dll", nullptr,
                                          LOAD_LIBRARY_SEARCH_SYSTEM32);
    if (loaded == nullptr) {
        return false;
    }
    FreeLibrary(loaded);
    return true;
}

std::wstring BundledTerminalPath() {
    const std::wstring directory = LauncherDirectory();
    if (directory.empty()) {
        return std::wstring();
    }
    std::wstring path = directory + L"\\" + kBundledTerminal;
    if (!FileExists(path) || !VisualCppRuntimePresent()) {
        return std::wstring();
    }
    return path;
}

bool StartDetached(std::wstring command) {
    STARTUPINFOW startup;
    ZeroMemory(&startup, sizeof(startup));
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process;
    ZeroMemory(&process, sizeof(process));
    // CreateProcessW may write into the command line it is handed, which is
    // why this takes the string by value rather than by const reference.
    if (!CreateProcessW(nullptr, command.data(), nullptr, nullptr,
                        FALSE, 0, nullptr, nullptr, &startup, &process)) {
        return false;
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return true;
}

// Reopen ourselves in a terminal that can actually run the CLI, and let this
// process end. Only the bare interactive launch is escalated: with arguments
// the caller is already sitting in a terminal they chose, and wt.exe's own
// command line treats `;` as a command separator, so forwarding arbitrary
// arguments through it would corrupt them.
//
// Order of preference:
//   1. The user's own Windows Terminal, through the profile the installer
//      registered as a fragment. Their settings, their updates, their tabs.
//   2. The copy bundled with this product, in portable mode. This is the
//      Windows 10 case: no Terminal is installed, conhost cannot deliver a
//      mouse click to a WSL process at all, and there is nothing to install
//      from a machine that may have no Store access.
//   3. Neither — run in place and let PrepareConsole salvage what it can.
bool RelaunchInBetterTerminal() {
    if (!ReadEnvironment(L"WT_SESSION").empty()) {
        return false;                       // already in a terminal
    }
    if (!ReadEnvironment(L"LAINTAS_NO_WT").empty()) {
        return false;                       // deliberate opt-out
    }

    wchar_t found[MAX_PATH];
    if (TerminalProfileInstalled()
            && SearchPathW(nullptr, L"wt.exe", nullptr, MAX_PATH, found,
                           nullptr) != 0) {
        std::wstring command = L"wt.exe -p \"";
        command += kTerminalProfile;
        command += L"\"";
        if (StartDetached(command)) {
            return true;
        }
    }

    const std::wstring bundled = BundledTerminalPath();
    if (!bundled.empty()) {
        // Portable mode: its settings sit next to the executable and name our
        // profile as the default, so it needs no arguments and cannot read or
        // write the user's own Terminal configuration.
        if (StartDetached(L"\"" + bundled + L"\"")) {
            return true;
        }
    }
    return false;
}

void PrintLaunchError(HRESULT result, const std::wstring& distribution) {
    std::fwprintf(
        stderr,
        L"laintas-cli: could not start the private WSL distribution '%ls' "
        L"(HRESULT 0x%08lX).\n"
        L"Run the Laintas Windows installer to install or repair it.\n",
        distribution.c_str(), static_cast<unsigned long>(result));
}

}  // namespace

int wmain(int argc, wchar_t** argv) {
    const std::wstring distribution = ReadDistributionName();
    if (!WslIsDistributionRegistered(distribution.c_str())) {
        std::fwprintf(
            stderr,
            L"laintas-cli: the private WSL distribution '%ls' is not installed.\n"
            L"Run the Laintas CLI Windows installer, then try again.\n",
            distribution.c_str());
        return 2;
    }

    if (argc == 1 && RelaunchInBetterTerminal()) {
        return 0;
    }

    // TERM is what tells the Linux side it may draw. WslLaunchInteractive
    // hands over the Windows environment, which has no TERM in it, and an
    // application that finds none assumes a terminal that can do nothing —
    // no colour, no cursor addressing, no mouse. Anything the user set
    // deliberately wins.
    std::wstring term = ReadEnvironment(L"LAINTAS_TERM");
    if (term.empty()) {
        term = L"xterm-256color";
    }
    std::wstring command = L"exec env TERM=";
    command += PosixQuote(term);
    // LAINTAS_HOST is how the Linux side knows it is the Windows product
    // rather than a Linux install, which is what turns mouse reporting on by
    // default. Keying that off the distribution name instead would silently
    // stop working for anyone who set LAINTAS_WSL_DISTRO.
    command += L" COLORTERM=truecolor LAINTAS_HOST=windows ";
    command += PosixQuote(kLinuxExecutable);
    for (int index = 1; index < argc; ++index) {
        command.push_back(L' ');
        command += PosixQuote(argv[index]);
    }

    const ConsoleState console = PrepareConsole();
    // After PrepareConsole, so virtual-terminal processing is on and these
    // are interpreted rather than printed.
    ClearInheritedTerminalModes(console.output);
    DWORD exit_code = 1;
    const HRESULT result = WslLaunchInteractive(
        distribution.c_str(), command.c_str(), TRUE, &exit_code);
    RestoreConsole(console);

    if (FAILED(result)) {
        PrintLaunchError(result, distribution);
        return 1;
    }
    return static_cast<int>(exit_code);
}
