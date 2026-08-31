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

namespace {

constexpr wchar_t kDefaultDistribution[] = L"Laintas-CLI";
constexpr wchar_t kLinuxExecutable[] = L"/usr/local/bin/laintas-cli";
constexpr wchar_t kTerminalProfile[] = L"Laintas CLI";

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
// terminal Windows picks. On Windows 10 that is conhost, whose defaults are
// wrong for a full-screen TUI in three separate ways, none of which the Linux
// side can do anything about:
//
//   * QuickEdit mode swallows every mouse event to drive its own rectangle
//     selection, so mouse reporting never reaches the application. This is
//     the whole reason clicking does nothing.
//   * Virtual-terminal processing is off, so colours, cursor movement and
//     anything else expressed as an escape sequence arrive as literal text.
//   * The default font is a raster font that has no box-drawing or CJK
//     glyphs, which is what turns a drawn frame into a row of hollow boxes.
//
// Windows Terminal gets all three right on its own; setting them there is
// harmless, so this is not conditional on which terminal we are in.

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
    // A raster font reports family 0 and an empty face name. Leave a font the
    // user chose deliberately alone; only replace the unusable default.
    if (font.FontFamily != 0 && font.FaceName[0] != L'\0') {
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
        state.restore_code_pages =
            SetConsoleCP(CP_UTF8) && SetConsoleOutputCP(CP_UTF8);
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

// Reopen ourselves in Windows Terminal, under the profile the installer
// registered, and let this process end. Only the bare interactive launch is
// escalated: with arguments the caller is already sitting in a terminal they
// chose, and wt.exe's own command line treats `;` as a command separator, so
// forwarding arbitrary arguments through it would corrupt them.
bool RelaunchInWindowsTerminal() {
    if (!ReadEnvironment(L"WT_SESSION").empty()) {
        return false;                       // already there
    }
    if (!ReadEnvironment(L"LAINTAS_NO_WT").empty()) {
        return false;                       // deliberate opt-out
    }
    if (!TerminalProfileInstalled()) {
        return false;                       // nothing to select; stay put
    }
    wchar_t found[MAX_PATH];
    if (SearchPathW(nullptr, L"wt.exe", nullptr, MAX_PATH, found, nullptr) == 0) {
        return false;                       // Windows Terminal is not present
    }

    std::wstring command = L"wt.exe -p \"";
    command += kTerminalProfile;
    command += L"\"";

    STARTUPINFOW startup;
    ZeroMemory(&startup, sizeof(startup));
    startup.cb = sizeof(startup);
    PROCESS_INFORMATION process;
    ZeroMemory(&process, sizeof(process));
    // CreateProcessW may write into the command line it is handed, which is
    // why this is a buffer we own rather than a literal.
    if (!CreateProcessW(nullptr, command.data(), nullptr, nullptr,
                        FALSE, 0, nullptr, nullptr, &startup, &process)) {
        return false;                       // fall through and run in place
    }
    CloseHandle(process.hThread);
    CloseHandle(process.hProcess);
    return true;
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

    if (argc == 1 && RelaunchInWindowsTerminal()) {
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
    command += L" COLORTERM=truecolor ";
    command += PosixQuote(kLinuxExecutable);
    for (int index = 1; index < argc; ++index) {
        command.push_back(L' ');
        command += PosixQuote(argv[index]);
    }

    const ConsoleState console = PrepareConsole();
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
