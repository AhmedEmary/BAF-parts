# Spec fixtures

XML transcribed **verbatim** from the LexCom DMS Protocol Specification 3.1
(`20260423 LexCom_interface_description_3.1_EN.pdf`):

| File | Source |
| --- | --- |
| `commands_available_request.xml` | spec p.5 |
| `commands_available_response_expected.xml` | spec p.5 (LexCom's own sample, NOT ours - it advertises `stock-information`, which we defer) |
| `order_submit_request.xml` | spec pp.12-15 |
| `order_submit_response_expected.xml` | spec p.15 |
| `order_append_request.xml` | spec pp.26-28 |
| `order_append_response_expected.xml` | spec p.28 |
| `order_list_request.xml` | spec p.16 |

These are the closest available stand-in for what LexCom's QA phase will send.
Do not "tidy" them: their value is that nobody on this side wrote them, so they
exercise fields our hand-written payloads never touch (`total-article`,
`total-labour`, `total-all`, discount attributes, multi-payment, `extension`).

## Test-client captures (`jar/`)

Request bodies captured **on the wire** from LexCom's own test client
(`LexcomDMSSpecificationClient_3.1.8.jar`) during its "Check specification
conformity" run, byte for byte (4-space JAXB pretty print, `standalone="yes"`).
Only the three richest requests are kept; `test_jar_conformity.py` derives the
client's other variants and its `X_UNKNOWN_X` error requests from them.

| File | Client request |
| --- | --- |
| `jar/order_list_request.xml` | order-list (dealer, country, brand only) |
| `jar/order_submit_request.xml` | order-submit "with payment, item and labour" |
| `jar/order_append_request.xml` | order-append "with payment, item and labour" |

Quirks they preserve: both items carry `item-id` 0, the labour item has only an
`operation-id` (no units), and no `customer-number` is sent. commands-available
is sent as `version="0.0"` and is inlined in the test.

## LexCom schemas (`xsd/`)

The response schemas plus their includes, copied **verbatim** from
`splitted-files/` inside LexCom's test client
(`LexcomDMSSpecificationClient_3.1.8.jar`). The client validates every response
against these, so `LexcomCommon.assert_lexcom_schema` does the same in the
tests. Only the response side is kept; request schemas are not needed.
