#[cfg(windows)]
mod native {
    use std::os::windows::io::AsRawHandle;
    use std::process::Child;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::System::JobObjects::{CreateJobObjectW, SetInformationJobObject, AssignProcessToJobObject, JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE};
    pub struct Job(HANDLE);
    unsafe impl Send for Job {}
    impl Job {
        pub fn attach(child: &Child) -> Result<Self, String> {
            unsafe {
                let handle = CreateJobObjectW(std::ptr::null(), std::ptr::null());
                if handle.is_null() { return Err(std::io::Error::last_os_error().to_string()); }
                let job = Self(handle);
                let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = std::mem::zeroed();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                if SetInformationJobObject(handle, JobObjectExtendedLimitInformation, &limits as *const _ as *const _, std::mem::size_of_val(&limits) as u32) == 0 || AssignProcessToJobObject(handle, child.as_raw_handle() as HANDLE) == 0 {
                    return Err(std::io::Error::last_os_error().to_string());
                }
                Ok(job)
            }
        }
    }
    impl Drop for Job { fn drop(&mut self) { unsafe { CloseHandle(self.0); } } }
}
#[cfg(windows)]
pub use native::Job;
#[cfg(not(windows))]
pub struct Job;
#[cfg(not(windows))]
impl Job { pub fn attach(_: &std::process::Child) -> Result<Self,String> { Err("Build and run the desktop host on Windows".into()) } }
