# Amazon RDS CA bundle

`ap-northeast-2-bundle.pem` is the Amazon RDS root CA bundle for the Seoul
Region. It is a public trust anchor, not a secret, and is pinned in the
application image so CloudFormation does not need to transport it as a
parameter or Secrets Manager value.

- Source: <https://truststore.pki.rds.amazonaws.com/ap-northeast-2/ap-northeast-2-bundle.pem>
- AWS documentation: <https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/UsingWithRDS.SSL.html>
- Retrieved: 2026-08-12
- File SHA-256: `913fb5b814f17af79d4c1622584a8d0ceddf5b0d76fe353d0c7d1186cdd6b229`

Root certificate SHA-256 fingerprints:

- ECC384 G1: `F4:7A:58:4F:42:F3:D9:FD:1E:0F:89:08:AE:65:A6:7E:C7:CC:50:34:0A:61:68:71:56:3B:DE:5D:88:9A:9C:C0`
- RSA2048 G1: `E8:DE:13:BF:96:65:BA:1A:29:67:00:80:28:FF:E4:F9:B4:F0:A6:DE:F6:E3:CA:F7:6E:0D:49:6C:D6:FA:CF:FC`
- RSA4096 G1: `00:FF:49:C0:8B:7D:5D:C5:33:28:78:49:86:58:09:48:D0:6E:E9:B2:F0:F1:E6:B6:EA:E7:0C:CD:ED:F5:F7:57`

When AWS publishes a replacement, download it only from the official
truststore URL, inspect every certificate, update the fingerprints and file
digest above, and run the SSL and CDK tests before building a new immutable
container image.
