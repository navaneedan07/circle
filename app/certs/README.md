# Code-signing certificates

This folder is **gitignored**. A signing key must never be committed.

## Current certificate (development only)

`circle-dev.pfx` is a **self-signed** certificate, generated with:

```powershell
$cert = New-SelfSignedCertificate -Type CodeSigningCert `
  -Subject 'CN=Circle, O=Circle, C=IN' -KeyUsage DigitalSignature `
  -FriendlyName 'Circle Code Signing' -CertStoreLocation Cert:\CurrentUser\My `
  -NotAfter (Get-Date).AddYears(3)
$pw = ConvertTo-SecureString -String '<password>' -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath certs/circle-dev.pfx -Password $pw
```

It produces a valid signature, but the chain ends in an untrusted root, so
Windows reports the publisher as unknown and SmartScreen still warns. It is
fine for local testing and nothing else.

## Replacing it with a real certificate

1. Buy a code-signing certificate (an OV or EV certificate from a CA such as
   DigiCert, Sectigo or SSL.com). EV certificates give the most immediate
   SmartScreen reputation.
2. Put the `.pfx` here (or anywhere outside the repo) and point
   `win.signtoolOptions.certificateFile` at it in `app/package.json`.
3. Build with the password in the environment, never in a file:

   ```bash
   CSC_KEY_PASSWORD=<password> npm run dist
   ```

For CI, pass the certificate as a base64 secret and use `CSC_LINK` +
`CSC_KEY_PASSWORD` instead of a file on disk.

## Verify a build

```powershell
Get-AuthenticodeSignature .\release\Circle-0.2.0-windows-x64.exe |
  Format-List Status, StatusMessage, SignerCertificate, TimeStamperCertificate
```

`Status` should read `Valid` once a real, trusted certificate is used.
