"""
Camelot 3PL SOAP client — minimal version for order sync checking.
Credentials are passed in at construction time (use st.secrets in Streamlit).
"""

from xml.etree import ElementTree as ET

import requests
from requests.auth import HTTPBasicAuth

_NS = "urn:microsoft-dynamics-schemas/codeunit/TPLWebServiceInt"
_NS_SOAP = "http://schemas.xmlsoap.org/soap/envelope/"

_ENVELOPE = """\
<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <{action} xmlns="{ns}">
      {params}
    </{action}>
  </soap:Body>
</soap:Envelope>"""


class CamelotError(Exception):
    pass


class CamelotClient:
    def __init__(self, soap_url: str, username: str, password: str,
                 interface_profile: str = "", client_code: str = "", trading_partner: str = ""):
        self.soap_url = soap_url
        self.auth = HTTPBasicAuth(username, password)
        self.profile = interface_profile
        self.client = client_code
        self.partner = trading_partner

    def _call(self, action: str, params: dict) -> ET.Element:
        param_xml = "\n      ".join(f"<{k}>{v}</{k}>" for k, v in params.items())
        envelope = _ENVELOPE.format(action=action, ns=_NS, params=param_xml)
        headers = {
            "Content-Type": "text/xml; charset=utf-8",
            "SOAPAction": f'"{_NS}"',
        }
        resp = requests.post(
            self.soap_url,
            data=envelope.encode("utf-8"),
            headers=headers,
            auth=self.auth,
            timeout=120,
        )

        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError:
            resp.raise_for_status()
            raise

        body = root.find(f"{{{_NS_SOAP}}}Body")

        fault = body.find(f"{{{_NS_SOAP}}}Fault")
        if fault is not None:
            msg = fault.findtext("faultstring") or ET.tostring(fault, encoding="unicode")
            raise CamelotError(msg)

        if not resp.ok:
            resp.raise_for_status()

        return list(body)[0]

    def _text(self, element: ET.Element, tag: str) -> str:
        return (
            element.findtext(f".//{{{_NS}}}{tag}")
            or element.findtext(f".//{tag}")
            or ""
        )

    def get_order_status(self, order_number: str, doc_type: int = 0) -> dict:
        """Returns tracking_number and status for a single order by internal doc number."""
        resp = self._call("GetOrderStatus", {
            "pDocType": doc_type,
            "pDocument": order_number,
            "pTrackingNumber": "",
            "pStatus": "",
        })
        return {
            "tracking_number": self._text(resp, "pTrackingNumber"),
            "status": self._text(resp, "pStatus"),
        }

    def get_shipped_orders(self, begin_date: str, end_date: str) -> dict[str, dict]:
        """Fetch all shipped orders for a date range via GetOrderStatusDateRange.
        Date format: YYYY-MM-DD
        Returns dict keyed by OrderRefNumber (e.g. '#SSUS48564') with tracking and status.
        """
        resp = self._call("GetOrderStatusDateRange", {
            "pInterfaceProfile": self.profile,
            "pClient": self.client,
            "pTradingPartner": self.partner,
            "pXMLDoc": "",
            "pBeginDate": begin_date,
            "pEndDate": end_date,
        })

        xml_string = self._text(resp, "pXMLDoc")
        if not xml_string:
            return {}

        root = ET.fromstring(xml_string)
        _ns = "urn:microsoft-dynamics-nav/xmlports/x37036604"

        results = {}
        for order in root:
            ref = order.findtext(f"{{{_ns}}}OrderRefNumber") or ""
            tracking = order.findtext(f"{{{_ns}}}ProNumber") or ""
            status = order.findtext(f"{{{_ns}}}OrderStatus") or ""
            if ref:
                if ref not in results or (tracking and not results[ref]["tracking_number"]):
                    results[ref] = {"tracking_number": tracking, "status": status}
        return results

    def test_connection(self) -> str:
        resp = self._call("TestConnection", {})
        return self._text(resp, "return_value")
