# RegistryTool Security Process and Policy
This document provides the details on the RegistryTool security policy and details the process surrounding security handling including a how to report a security vulnerability. 

* [Reporting a Vulnerability](#reporting-a-vulnerability)
    * [When To Send a Report](#when-to-send-a-report)
    * [What To Include In a Report](#what-to-include-in-a-report)
    * [When Not To Send a Report](#when-not-to-send-a-report)
    * [Security Vulnerability Response](#security-vulnerability-response)
    * [Public Disclosure](#public-disclosure)
* [Credits](#credits)

## Reporting a Vulnerability

I am extremely grateful for security researchers and users who report vulnerabilities to the RegistryTool project. All reports are thouroughly investigated and discussed with the person or organization that have reported it.

To make a report plese use the GitHub Security Vulnerability Disclosure process:

- [RegistryTool CLI Vulnerability Report](https://github.com/toddysm/registrytool/security/advisories/new)

### When To Send a Report
You think you have found a vulnerability in the RegistryTool or a dependency of the RegistryTool. 

### What To Include In a Report
The more details are included in the report, the easier will be for me to understand the vulnerability and provide mitigations. The vulnerability disclosure template requires the following information:
- Short summary of the problem
    
    This should be a single sentence that clearly summarize the vulnerability.
- Detailed description of the vulnerability
    
    Provide all possible details about the vulnerability. Versions of binaries, pointing to incriminated source code, environment details etc. are essential to understand the vulnerability and its impact.
- Proof of Concept (PoC) steps
    
    Detailed steps to reproduce the vulnerabilitt. This should include CLI commands, specific configuration details, library calls, etc.
- Impact
    
    Describe the impact of the vulnerability and who the impacted audience is.
    
Feel free to include anything else that you deem relevant for better understanding of the vulnerability.

### When Not To Send a Report
- If a vulnerability has been found in an application that uses the RegistryTool. Instead, contact the maintaners of the respective application.
- You are looking for help applying security updates.

For guidance on securing the RegistryTool, please see the [documentation](https://github.com/toddysm/registrytool/docs).

### Security Vulnerability Response
Each report will be reviewed and receipt acknowledged within 3 business days. This will set off the security review process detailed below.

Any vulnerability information shared with me stays within the RegistrYTool project itslef and will not be shared with others unless it is necessary to fix the issue. Information is shared only on a need to know basis.

I ask that vulnerability reporter(s) act in good faith by not disclosing the issue to others. And I strive to act in good faith by acting swiftly, and by justly crediting the vulnerability reporter(s) in writing.

As the security issue moves through triage, identification, and release the reporter of the security vulnerability will be notified. Additional questions about the vulnerability may also be asked of the reporter.

### Public Disclosure
A public disclosure of security vulnerabilities is released alongside release updates or details that fix the vulnerability. I try to fully disclose vulnerabilities once a mitigation strategy is available. My goal is to perform a release and public disclosure quickly and in a timetable that works well for users. For example, a release may be ready on a Friday but for the sake of users may be delayed to the following Monday.

CVEs will be assigned to vulnerabilities. Due to the process and time it takes to obtain a CVE ID, disclosures will happen first. Once the disclosure is public the process will begin to obtain a CVE ID. Once the ID has been assigned the disclosure will be updated.

If the vulnerability reporter would like their name and details shared as part of the disclosure process I am happy to do so. I will ask permission and for the way the reporter would like to be identified. I appreciate vulnerability reports and would like to credit reporters if they would like the credit.

Vulnerability disclosures are published in the Security Advisories sections in the repository. The disclosures will contain an overview, details about the vulnerability, a fix for the vulnerability that will typically be an update, and optionally a workaround if one is available.

Disclosures will be published on the same day as a release fixing the vulnerability after the release is published.

Here are the links to security advisories:

- [RegistryTool CLI Security Advisories](https://github.com/toddysm/registrytool/security/advisories)

## Credits
We would like to give credit to the [Helm Community](https://github.com/helm/community) for using their security process and policy as an example.