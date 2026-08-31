#define UNICODE
#define _UNICODE

#include <windows.h>
#include <wslapi.h>

#include <cstdio>
#include <string>

namespace {

constexpr wchar_t kDefaultDistribution[] = L"Laintas-CLI";
constexpr wchar_t kLinuxExecutable[] = L"/usr/local/bin/laintas-cli";

std::wstring ReadDistributionName() {
    DWORD required = GetEnvironmentVariableW(L"LAINTAS_WSL_DISTRO", nullptr, 0);
    if (required == 0) {
        return kDefaultDistribution;
    }
    std::wstring value(required, L'\0');
    DWORD written = GetEnvironmentVariableW(
        L"LAINTAS_WSL_DISTRO", value.data(), required);
    if (written == 0 || written >= required) {
        return kDefaultDistribution;
    }
    value.resize(written);
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
            L"Run install.cmd from the Windows package, then try again.\n",
            distribution.c_str());
        return 2;
    }

    std::wstring command = L"exec ";
    command += PosixQuote(kLinuxExecutable);
    for (int index = 1; index < argc; ++index) {
        command.push_back(L' ');
        command += PosixQuote(argv[index]);
    }
    DWORD exit_code = 1;
    const HRESULT result = WslLaunchInteractive(
        distribution.c_str(), command.c_str(), TRUE, &exit_code);
    if (FAILED(result)) {
        PrintLaunchError(result, distribution);
        return 1;
    }
    return static_cast<int>(exit_code);
}
