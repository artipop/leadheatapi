"""Fetch archive metadata and archive file from zakupki.gov.ru SOAP API.

Implementation follows the workflow from:
https://vc.ru/dev/1730048-poluchaem-dannye-s-zakupkigovru-cherez-python-poshagovoe-rukovodstvo
And also about certs https://ru.stackoverflow.com/q/1537599
"""

import argparse
import datetime as dt
import os
import uuid
from pathlib import Path
from typing import Any

import requests
import xmltodict

DEFAULT_ENDPOINT = "https://int.zakupki.gov.ru/eis-integration/services/getDocsIP"


def build_request_xml(
        token: str,
        reestr_number: str,
        subsystem_type: str,
        mode: str,
) -> str:
    request_id = str(uuid.uuid4())
    created_time = dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    return f"""<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/" xmlns:ws="http://zakupki.gov.ru/fz44/get-docs-ip/ws">
  <soapenv:Header>
    <individualPerson_token>{token}</individualPerson_token>
  </soapenv:Header>
  <soapenv:Body>
    <ws:getDocsByReestrNumberRequest>
      <index>
        <id>{request_id}</id>
        <createDateTime>{created_time}</createDateTime>
        <mode>{mode}</mode>
      </index>
      <selectionParams>
        <subsystemType>{subsystem_type}</subsystemType>
        <reestrNumber>{reestr_number}</reestrNumber>
      </selectionParams>
    </ws:getDocsByReestrNumberRequest>
  </soapenv:Body>
</soapenv:Envelope>"""


def _find_key_recursive(data: Any, key: str) -> str | None:
    if isinstance(data, dict):
        for current_key, value in data.items():
            if current_key == key and isinstance(value, str):
                return value.strip()
            found = _find_key_recursive(value, key)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_key_recursive(item, key)
            if found:
                return found
    return None


def extract_archive_url(xml_text: str) -> str:
    # TODO: or we can use `xml.etree.ElementTree` instead (depending on what is suitable for asyncio)
    payload = xmltodict.parse(xml_text)
    url_archive = payload['soap:Envelope']['soap:Body']['ns2:getDocsByOrgRegionResponse']['dataInfo'][
        'archiveUrl']
    return url_archive
    # archive_url = _find_key_recursive(payload, "archiveUrl")
    # if archive_url:
    #     return archive_url
    #
    # fault = _find_key_recursive(payload, "faultstring")
    # if fault:
    #     raise RuntimeError(f"SOAP fault: {fault}")
    # raise RuntimeError("archiveUrl not found in SOAP response")


def request_archive_url(
        endpoint: str,
        xml_data: str,
        soap_timeout: int,
        verify: str | None,
) -> str:
    headers = {"Content-Type": "text/xml; charset=utf-8"}
    response = requests.post(
        endpoint,
        data=xml_data,
        headers=headers,
        timeout=soap_timeout,
        verify=verify,
    )
    response.raise_for_status()
    return response.text


def download_archive(
        url: str,
        token: str,
        output_path: Path,
        download_timeout: int,
        verify: str | None,
) -> None:
    headers = {"individualPerson_token": token}
    response = requests.get(url, headers=headers, timeout=download_timeout, verify=verify)
    response.raise_for_status()
    output_path.write_bytes(response.content)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Get archive.zip from zakupki.gov.ru by reestr number (getDocsByReestrNumberRequest)."
    )
    parser.add_argument(
        "--token",
        default=os.getenv("ZAKUPKI_TOKEN"),
        help="Token from zakupki.gov.ru (or set ZAKUPKI_TOKEN env var).",
    )
    parser.add_argument(
        "--reestr-number",
        required=True,
        help="Reestr number, e.g. 0888200000224000038.",
    )
    parser.add_argument(
        "--subsystem-type",
        default="PRIZ",
        help="subsystemType in selectionParams. Default: PRIZ.",
    )
    parser.add_argument(
        "--mode",
        default="PROD",
        choices=("PROD", "TEST"),
        help="Request mode in index. Default: PROD.",
    )
    parser.add_argument(
        "--endpoint",
        default=DEFAULT_ENDPOINT,
        help=f"SOAP endpoint. Default: {DEFAULT_ENDPOINT}",
    )
    parser.add_argument(
        "--response-xml",
        default="soap_response.xml",
        help="Path to save SOAP response XML. Default: soap_response.xml",
    )
    parser.add_argument(
        "--out",
        default="archive.zip",
        help="Path to save archive file. Default: archive.zip",
    )
    parser.add_argument(
        "--soap-timeout",
        type=int,
        default=30,
        help="SOAP request timeout in seconds. Default: 30.",
    )
    parser.add_argument(
        "--download-timeout",
        type=int,
        default=120,
        help="Archive download timeout in seconds. Default: 120.",
    )
    parser.add_argument(
        "--ca-bundle",
        default=os.getenv("ZAKUPKI_CA_BUNDLE"),
        help=(
            "Path to custom PEM CA bundle. By default, certifi is extended with "
            "Russian trusted CA certificates from scripts/russiantrustedca and "
            "scripts/linux_russian_trusted_root_ca_pem. Can also be set via "
            "ZAKUPKI_CA_BUNDLE."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.token:
        raise SystemExit("Token is required: pass --token or set ZAKUPKI_TOKEN")

    xml_data = build_request_xml(
        token=args.token,
        reestr_number=args.reestr_number,
        subsystem_type=args.subsystem_type,
        mode=args.mode,
    )

    print("Sending SOAP request...")
    soap_response = request_archive_url(
        endpoint=args.endpoint,
        xml_data=xml_data,
        soap_timeout=args.soap_timeout,
        verify=args.ca_bundle,
    )
    response_path = Path(args.response_xml)
    response_path.write_text(soap_response, encoding="utf-8")
    print(f"SOAP response saved: {response_path}")

    archive_url = extract_archive_url(soap_response)
    print(f"Archive URL found: {archive_url}")

    output_path = Path(args.out)
    print("Downloading archive...")
    download_archive(
        url=archive_url,
        token=args.token,
        output_path=output_path,
        download_timeout=args.download_timeout,
        verify=args.ca_bundle,
    )
    print(f"Archive saved: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
