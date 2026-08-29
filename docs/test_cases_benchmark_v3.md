# NexTripAI End-to-End GraphRAG Benchmark v3

> Bộ 500 tình huống dùng chung để đánh giá các phiên bản GraphRAG. Benchmark v2 được giữ nguyên để đối chiếu kết quả lịch sử.

## Thiết kế

- Nhóm so sánh chung: 400 tình huống dùng để so sánh công bằng giữa các phiên bản.
- Nhóm năng lực mở rộng: 100 tình huống về quan hệ, lập kế hoạch và hội thoại.
- Tất cả phiên bản đều chạy đủ 500 tình huống; năng lực chưa được hỗ trợ được báo cáo riêng.
- Câu hỏi được viết theo tình huống du lịch thực tế; tiêu chí kỹ thuật được giữ riêng để chấm tự động.

| Nhóm | So sánh chung | Năng lực mở rộng | Tổng |
|---|---:|---:|---:|
| A. Tìm kiếm và hỏi đáp thông tin | 120 | 0 | 120 |
| B. Lọc và đề xuất | 80 | 20 | 100 |
| C. Quan hệ và suy luận | 50 | 30 | 80 |
| D. Lập kế hoạch chuyến đi | 60 | 20 | 80 |
| E. Hội thoại nhiều lượt | 50 | 10 | 60 |
| F. Tình huống khó, thiếu thông tin và lỗi cũ | 40 | 20 | 60 |
| **Tổng** | **400** | **100** | **500** |

## Quy tắc đánh giá

1. Mỗi phiên bản nhận cùng câu hỏi, dữ liệu và cấu hình khi cần so sánh công bằng.
2. Câu trả lời cần đúng nhu cầu, đúng địa điểm, có cơ sở và giữ được bối cảnh hội thoại.
3. Case không đạt nếu bỏ sót yêu cầu bắt buộc, gợi ý sai địa điểm hoặc tự thêm thông tin chưa được xác nhận.
4. Báo cáo riêng nhóm so sánh chung 400 case và toàn bộ 500 case.
5. Trường hợp phiên bản chưa hỗ trợ một năng lực phải được ghi nhận riêng, không xem như câu trả lời đúng.

## A. Tìm kiếm và hỏi đáp thông tin

| ID | Tình huống / câu hỏi của người dùng | Kết quả mong đợi |
|---|---|---|
| A-001 | Ở Đà Nẵng có bao nhiêu điểm tham quan để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-002 | Ở Đà Nẵng có bao nhiêu nơi lưu trú để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-003 | Ở Đà Nẵng có bao nhiêu nhà hàng để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-004 | Ở Đà Nẵng có bao nhiêu quán cafe để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-005 | Ở Đà Nẵng có bao nhiêu địa điểm nightlife để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-006 | Ở Quy Nhơn có bao nhiêu điểm tham quan để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-007 | Ở Quy Nhơn có bao nhiêu nơi lưu trú để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-008 | Ở Quy Nhơn có bao nhiêu nhà hàng để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-009 | Ở Quy Nhơn có bao nhiêu quán cafe để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-010 | Ở Quy Nhơn có bao nhiêu địa điểm nightlife để tôi lựa chọn? | Cho biết đúng số lượng lựa chọn theo thành phố và loại hình được hỏi. |
| A-011 | Bảo tàng điêu khắc Chăm ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-012 | Khu du lịch Suối Lương ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-013 | Cù Lao Chàm ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-014 | Đỉnh Bàn Cờ ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-015 | Chợ đêm Sơn Trà ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-016 | Công viên nước Mikazuki ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-017 | Minh House ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-018 | A Little Hoi An Homestay Đà Nẵng ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-019 | 4My Little Pig Home ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-020 | Hana Homestay Danang ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-021 | An Homestay Danang ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-022 | Dreamy House ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-023 | Hải Sản Năm Đảnh ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-024 | Zé Food ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-025 | Maru Food & Drinks ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-026 | Quán Ăn Dimsum ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-027 | Bún Đậu Mắm Tôm Cô Thường ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-028 | Ăn Vặt Tre Xanh ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-029 | Gong Cha ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-030 | Wonderlust Cafe & Bakery ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-031 | Cloud Garden Coffee ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-032 | OM Herbal Tea & Coffee ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-033 | Cafe de Ante ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-034 | Pavilion Garden ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-035 | Sky 36 ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-036 | TV Club ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-037 | On The Radio Bar ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-038 | Memory Lounge Bar & Restaurant ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-039 | New Phương Đông Club ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-040 | OQ Lounge Pub DnD ở đâu? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-041 | Giờ hoạt động của Bảo tàng điêu khắc Chăm là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-042 | Giờ hoạt động của Khu du lịch Suối Lương là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-043 | Giờ hoạt động của Cù Lao Chàm là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-044 | Giờ hoạt động của Đỉnh Bàn Cờ là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-045 | Giờ hoạt động của Furama Villas Danang là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-046 | Giờ hoạt động của Khu nghỉ dưỡng Furama Đà Nẵng là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-047 | Giờ hoạt động của Minh House là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-048 | Giờ hoạt động của Carol's Homestay là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-049 | Giờ hoạt động của Hải Sản Năm Đảnh là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-050 | Giờ hoạt động của Zé Food là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-051 | Giờ hoạt động của Maru Food & Drinks là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-052 | Giờ hoạt động của Quán Ăn Dimsum là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-053 | Giờ hoạt động của Gong Cha là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-054 | Giờ hoạt động của Wonderlust Cafe & Bakery là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-055 | Giờ hoạt động của Cloud Garden Coffee là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-056 | Giờ hoạt động của Sleeping Wood Café là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-057 | Giờ hoạt động của Sky 36 là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-058 | Giờ hoạt động của TV Club là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-059 | Giờ hoạt động của Apocalypse Beach Club là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-060 | Giờ hoạt động của On The Radio Bar là khi nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-061 | Bảo tàng điêu khắc Chăm được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-062 | Khu du lịch Suối Lương được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-063 | Cù Lao Chàm được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-064 | Furama Villas Danang được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-065 | Khu nghỉ dưỡng Furama Đà Nẵng được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-066 | Minh House được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-067 | Hải Sản Năm Đảnh được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-068 | Zé Food được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-069 | Maru Food & Drinks được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-070 | Wonderlust Cafe & Bakery được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-071 | Cloud Garden Coffee được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-072 | OM Herbal Tea & Coffee được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-073 | Sky 36 được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-074 | TV Club được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-075 | On The Radio Bar được đánh giá bao nhiêu điểm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-076 | Bảo tàng điêu khắc Chăm thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-077 | Khu du lịch Suối Lương thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-078 | Furama Villas Danang thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-079 | Khu nghỉ dưỡng Furama Đà Nẵng thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-080 | Hải Sản Năm Đảnh thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-081 | Zé Food thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-082 | Gong Cha thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-083 | Wonderlust Cafe & Bakery thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-084 | Sky 36 thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-085 | TV Club thuộc loại địa điểm nào? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-086 | Hilton Da Nang: là khách sạn mấy sao? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-087 | Khách sạn Hải Âu là khách sạn mấy sao? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-088 | Khách sạn Mường Thanh Quy Nhơn là khách sạn mấy sao? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-089 | Khách sạn Ly Kỳ Quy Nhơn là khách sạn mấy sao? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-090 | Nhà nghỉ Thanh Tùng Quy Nhơn là khách sạn mấy sao? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-091 | Thời điểm nào phù hợp để đến Bảo tàng điêu khắc Chăm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-092 | Thời điểm nào phù hợp để đến Khu du lịch Suối Lương? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-093 | Thời điểm nào phù hợp để đến Cù Lao Chàm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-094 | Thời điểm nào phù hợp để đến Đỉnh Bàn Cờ? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-095 | Thời điểm nào phù hợp để đến Đèo Hải Vân? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-096 | Nên dành bao lâu để tham quan Bảo tàng điêu khắc Chăm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-097 | Nên dành bao lâu để tham quan Khu du lịch Suối Lương? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-098 | Nên dành bao lâu để tham quan Cù Lao Chàm? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-099 | Nên dành bao lâu để tham quan Đỉnh Bàn Cờ? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-100 | Nên dành bao lâu để tham quan Đèo Hải Vân? | Trả lời đúng thông tin người dùng hỏi về địa điểm; không thêm chi tiết chưa được xác nhận. |
| A-101 | Vé vào Đàn Tế Đất Trời Ấn Sơn giá bao nhiêu? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-102 | Vé vào Chùa Bà Nước Mặn giá bao nhiêu? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-103 | Vé vào Chùa Minh Tịnh giá bao nhiêu? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-104 | Vé vào Thánh địa Mỹ Sơn giá bao nhiêu? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-105 | Một đêm ở Khách sạn Hải Âu có giá khoảng bao nhiêu? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-106 | Một đêm ở Khách sạn Mường Thanh Quy Nhơn có giá khoảng bao nhiêu? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-107 | Một đêm ở Khách sạn Hoàng Yến Quy Nhơn có giá khoảng bao nhiêu? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-108 | Một đêm ở Khách sạn Yến Vy Quy Nhơn có giá khoảng bao nhiêu? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-109 | Ăn tại Hướng Dương Quán thường tốn khoảng bao nhiêu mỗi người? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-110 | Ăn tại Bánh Hỏi Cháo Lòng Hồng Thanh thường tốn khoảng bao nhiêu mỗi người? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-111 | Ăn tại Quán Thuỳ thường tốn khoảng bao nhiêu mỗi người? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-112 | Ăn tại Bánh Xèo Anh Vũ thường tốn khoảng bao nhiêu mỗi người? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-113 | YQ Cafe có gì phù hợp với nhu cầu của khách? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-114 | Búp Station có gì phù hợp với nhu cầu của khách? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-115 | Quán cafe S- Blue Restaurant & Bar có gì phù hợp với nhu cầu của khách? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-116 | Book Cafe đẹp ở Quy Nhơn có gì phù hợp với nhu cầu của khách? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-117 | Surf bar Quy Nhơn thuộc loại hình nightlife nào? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-118 | Kyoto Club Lounge Quy Nhơn thuộc loại hình nightlife nào? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-119 | THƠM Cocktail Bar thuộc loại hình nightlife nào? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |
| A-120 | ROYAL CLUB thuộc loại hình nightlife nào? | Trả lời đúng thông tin được hỏi về địa điểm và diễn đạt dễ hiểu cho khách du lịch. |

## B. Lọc và đề xuất

| ID | Tình huống / câu hỏi của người dùng | Kết quả mong đợi |
|---|---|---|
| B-001 | Gợi ý bãi biển ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-002 | Gợi ý khu vui chơi ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-003 | Gợi ý địa điểm lịch sử ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-004 | Gợi ý địa điểm thiên nhiên ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-005 | Gợi ý địa điểm văn hóa ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-006 | Gợi ý điểm tham quan ít người biết ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-007 | Gợi ý điểm tham quan đẹp để chụp ảnh ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-008 | Gợi ý điểm tham quan lãng mạn ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-009 | Gợi ý điểm tham quan đáng trải nghiệm ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-010 | Gợi ý điểm tham quan phù hợp ngắm hoàng hôn ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-011 | Gợi ý điểm tham quan phù hợp gia đình ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-012 | Gợi ý điểm tham quan ở Đà Nẵng phù hợp khi trời nắng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-013 | Gợi ý điểm tham quan ở Đà Nẵng phù hợp khi trời nhiều mây | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-014 | Gợi ý các điểm tham quan được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-015 | Gợi ý bãi biển được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-016 | Gợi ý địa điểm thiên nhiên được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-017 | Gợi ý địa điểm lịch sử được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-018 | Gợi ý địa điểm văn hóa được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-019 | Gợi ý khu vui chơi được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-020 | Gợi ý bãi biển ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-021 | Gợi ý khu vui chơi ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-022 | Gợi ý địa điểm lịch sử ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-023 | Gợi ý địa điểm thiên nhiên ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-024 | Gợi ý địa điểm văn hóa ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-025 | Gợi ý điểm tham quan ít người biết ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-026 | Gợi ý hostel ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-027 | Gợi ý khách sạn ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-028 | Gợi ý resort ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-029 | Gợi ý villa ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-030 | Gợi ý căn hộ ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-031 | Gợi ý nơi lưu trú cao cấp ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-032 | Gợi ý nơi lưu trú có hồ bơi ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-033 | Gợi ý nơi lưu trú có view biển ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-034 | Gợi ý nơi lưu trú tiết kiệm ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-035 | Gợi ý khách sạn 5 sao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-036 | Gợi ý nơi lưu trú ở Đà Nẵng có hồ bơi | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-037 | Gợi ý nơi lưu trú ở Đà Nẵng có tiện ích wifi miễn phí | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-038 | Gợi ý nơi lưu trú ở Đà Nẵng có tiện ích phòng gia đình | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-039 | Gợi ý nơi lưu trú ở Đà Nẵng có lối đi thuận tiện ra biển | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-040 | Gợi ý nơi lưu trú ở Đà Nẵng có nhà hàng trong khuôn viên | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-041 | Gợi ý nơi lưu trú được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-042 | Gợi ý khách sạn ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-043 | Gợi ý resort ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-044 | Gợi ý nơi lưu trú cao cấp ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-045 | Gợi ý nơi lưu trú có hồ bơi ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-046 | Gợi ý nơi lưu trú có view biển ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-047 | Gợi ý nơi lưu trú tiết kiệm ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-048 | Gợi ý khách sạn 2 sao ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-049 | Gợi ý khách sạn 3 sao ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-050 | Gợi ý khách sạn 4 sao ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-051 | Gợi ý nhà hàng hải sản ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-052 | Gợi ý nhà hàng món Việt ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-053 | Gợi ý nhà hàng BBQ ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-054 | Gợi ý nhà hàng phổ biến ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-055 | Gợi ý nhà hàng có đặc sản địa phương ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-056 | Gợi ý nhà hàng tiết kiệm ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-057 | Gợi ý nhà hàng có view biển ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-058 | Gợi ý nhà hàng được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-059 | Gợi ý nhà hàng hải sản được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-060 | Gợi ý nhà hàng món Việt được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-061 | Gợi ý nhà hàng BBQ được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-062 | Gợi ý nhà hàng hải sản ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-063 | Gợi ý nhà hàng món Việt ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-064 | Gợi ý nhà hàng BBQ ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-065 | Gợi ý nhà hàng phổ biến ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-066 | Gợi ý nhà hàng có đặc sản địa phương ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-067 | Gợi ý nhà hàng tiết kiệm ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-068 | Gợi ý nhà hàng có view biển ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-069 | Gợi ý nhà hàng được đánh giá cao ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-070 | Gợi ý nhà hàng hải sản được đánh giá cao ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-071 | Gợi ý quán cafe ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-072 | Gợi ý quán trà ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-073 | Gợi ý cafe rooftop ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-074 | Gợi ý cafe phù hợp làm việc ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-075 | Gợi ý quán cà phê đặc sản ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-076 | Gợi ý quán cafe thư giãn ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-077 | Gợi ý quán cafe đẹp để chụp ảnh ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-078 | Gợi ý quán cafe có view biển ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-079 | Gợi ý quán cafe tiết kiệm ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-080 | Gợi ý quán cafe ở Đà Nẵng phù hợp để làm việc | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-081 | Gợi ý quán cafe ở Đà Nẵng phù hợp cho cặp đôi | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-082 | Gợi ý quán cafe ở Đà Nẵng phù hợp cho nhóm bạn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-083 | Gợi ý quán cafe được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-084 | Gợi ý quán cafe ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-085 | Gợi ý quán trà ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-086 | Gợi ý cafe rooftop ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-087 | Gợi ý cafe phù hợp làm việc ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-088 | Gợi ý quán cà phê đặc sản ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-089 | Gợi ý quán cafe thư giãn ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-090 | Gợi ý quán cafe đẹp để chụp ảnh ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-091 | Gợi ý bar ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-092 | Gợi ý pub ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-093 | Gợi ý club ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-094 | Gợi ý rooftop bar ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-095 | Gợi ý địa điểm nightlife phổ biến ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-096 | Gợi ý địa điểm nightlife ở rooftop ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-097 | Gợi ý địa điểm nightlife có nhạc sống ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-098 | Gợi ý địa điểm nightlife được đánh giá cao ở Đà Nẵng | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-099 | Gợi ý bar ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |
| B-100 | Gợi ý pub ở Quy Nhơn | Chỉ gợi ý những lựa chọn phù hợp với địa điểm và nhu cầu đã nêu; giải thích ngắn gọn vì sao phù hợp. |

## C. Quan hệ và suy luận

| ID | Tình huống / câu hỏi của người dùng | Kết quả mong đợi |
|---|---|---|
| C-001 | Tôi đang ở Bảo tàng điêu khắc Chăm, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-002 | Tôi đang ở Khu du lịch Suối Lương, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-003 | Tôi đang ở Đỉnh Bàn Cờ, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-004 | Tôi đang ở Đèo Hải Vân, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-005 | Tôi đang ở Cầu Rồng, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-006 | Tôi đang ở Chợ đêm Sơn Trà, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-007 | Tôi đang ở Da Nang Downtown, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-008 | Tôi đang ở Công viên nước Mikazuki, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-009 | Tôi đang ở Sơn Trà Tịnh Viên, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-010 | Tôi đang ở Bảo tàng Đồng Đình, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-011 | Tôi đang ở Động Huyền Không, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-012 | Tôi đang ở Chợ Cồn, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-013 | Tôi đang ở Chợ Hàn, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-014 | Tôi đang ở Bãi biển Mỹ Khê, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-015 | Tôi đang ở Bãi biển Non Nước, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-016 | Tôi đang ở Bãi biển Bắc Mỹ An, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-017 | Tôi đang ở Bãi biển Nam Ô, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-018 | Tôi đang ở Bãi biển Tiên Sa, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-019 | Tôi đang ở Bãi biển Làng Vân, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-020 | Tôi đang ở Bãi Biển Xuân Thiều, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-021 | Tôi đang ở Bãi biển Thanh Bình, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-022 | Tôi đang ở Bãi biển An Bàng, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-023 | Tôi đang ở Nhất Lâm Thủy Trang Trà, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-024 | Tôi đang ở Hồ Xanh, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-025 | Tôi đang ở Làng đá mỹ nghệ Non Nước, gần đó có điểm tham quan nào? | Gợi ý đúng những điểm tham quan thực sự ở gần vị trí người dùng đang đứng. |
| C-026 | Từ Bảo tàng điêu khắc Chăm đến Bảo tàng Chăm Đà Nẵng khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-027 | Từ Bảo tàng điêu khắc Chăm đến Cầu Rồng khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-028 | Từ Bảo tàng điêu khắc Chăm đến Sông Hàn khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-029 | Từ Bảo tàng điêu khắc Chăm đến Cầu Tình Yêu & Cá chép hóa rồng khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-030 | Từ Bảo tàng điêu khắc Chăm đến Chợ Hàn khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-031 | Từ Khu du lịch Suối Lương đến Bãi biển Làng Vân khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-032 | Từ Khu du lịch Suối Lương đến Đèo Hải Vân khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-033 | Từ Khu du lịch Suối Lương đến Bãi biển Nam Ô khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-034 | Từ Đỉnh Bàn Cờ đến Sơn Trà Tịnh Viên khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-035 | Từ Đỉnh Bàn Cờ đến Bảo tàng Đồng Đình khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-036 | Từ Đỉnh Bàn Cờ đến Bảo tàng Đồng Đình khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-037 | Từ Đỉnh Bàn Cờ đến Bán đảo Sơn Trà và Chùa Linh Ứng bãi Bụt khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-038 | Từ Đỉnh Bàn Cờ đến Hồ Xanh khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-039 | Từ Đèo Hải Vân đến Khu du lịch Suối Lương khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-040 | Từ Đèo Hải Vân đến Bãi biển Làng Vân khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-041 | Từ Cầu Rồng đến Cầu Tình Yêu & Cá chép hóa rồng khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-042 | Từ Cầu Rồng đến Chợ đêm Sơn Trà khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-043 | Từ Cầu Rồng đến Bảo tàng điêu khắc Chăm khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-044 | Từ Cầu Rồng đến Bảo tàng Chăm Đà Nẵng khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-045 | Từ Cầu Rồng đến Sông Hàn khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-046 | Từ Chợ đêm Sơn Trà đến Cầu Tình Yêu & Cá chép hóa rồng khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-047 | Từ Chợ đêm Sơn Trà đến Cầu Rồng khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-048 | Từ Chợ đêm Sơn Trà đến Sông Hàn khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-049 | Từ Chợ đêm Sơn Trà đến Bảo tàng điêu khắc Chăm khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-050 | Từ Chợ đêm Sơn Trà đến Bảo tàng Chăm Đà Nẵng khoảng bao xa? | Nêu đúng khoảng cách giữa hai địa điểm và diễn đạt rõ đây là khoảng cách xấp xỉ. |
| C-051 | Quanh Bảo tàng điêu khắc Chăm có địa điểm lịch sử nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-052 | Quanh Bảo tàng điêu khắc Chăm có địa điểm vui chơi, giải trí nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-053 | Quanh Khu du lịch Suối Lương có địa điểm bãi biển nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-054 | Quanh Khu du lịch Suối Lương có địa điểm vui chơi, giải trí nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-055 | Quanh Đỉnh Bàn Cờ có địa điểm vui chơi, giải trí nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-056 | Quanh Đỉnh Bàn Cờ có địa điểm lịch sử nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-057 | Quanh Đèo Hải Vân có địa điểm thiên nhiên nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-058 | Quanh Đèo Hải Vân có địa điểm bãi biển nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-059 | Quanh Cầu Rồng có địa điểm vui chơi, giải trí nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-060 | Quanh Cầu Rồng có địa điểm lịch sử nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-061 | Quanh Chợ đêm Sơn Trà có địa điểm vui chơi, giải trí nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-062 | Quanh Chợ đêm Sơn Trà có địa điểm lịch sử nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-063 | Quanh Da Nang Downtown có địa điểm vui chơi, giải trí nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-064 | Quanh Da Nang Downtown có địa điểm văn hóa nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-065 | Quanh Công viên nước Mikazuki có địa điểm bãi biển nào đáng ghé? | Chỉ chọn địa điểm vừa ở gần vừa đúng loại trải nghiệm người dùng yêu cầu. |
| C-066 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Bảo tàng Chăm Đà Nẵng, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 480 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-067 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Bảo tàng Chăm Đà Nẵng, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 740 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-068 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Bảo tàng Chăm Đà Nẵng, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 760 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-069 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Bảo tàng Chăm Đà Nẵng, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 870 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-070 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Cầu Rồng, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 310 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-071 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Cầu Rồng, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 430 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-072 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Cầu Rồng, tôi muốn ghé thêm một điểm lịch sử cách đó khoảng 480 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-073 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Cầu Rồng, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 630 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-074 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Sông Hàn, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 270 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-075 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Sông Hàn, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 570 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-076 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Sông Hàn, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 630 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-077 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Sông Hàn, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 640 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-078 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Cầu Tình Yêu & Cá chép hóa rồng, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 310 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-079 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Cầu Tình Yêu & Cá chép hóa rồng, tôi muốn ghé thêm một điểm vui chơi, giải trí cách đó khoảng 570 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |
| C-080 | Sau khi đi từ Bảo tàng điêu khắc Chăm đến Cầu Tình Yêu & Cá chép hóa rồng, tôi muốn ghé thêm một điểm lịch sử cách đó khoảng 760 m. Nơi nào phù hợp? | Gợi ý đúng một điểm phù hợp với loại trải nghiệm và khoảng cách đi tiếp mà người dùng đã nêu. |

## D. Lập kế hoạch chuyến đi

| ID | Tình huống / câu hỏi của người dùng | Kết quả mong đợi |
|---|---|---|
| D-001 | Đây là lần đầu tôi đến Đà Nẵng. Hãy sắp xếp lịch trình 1 ngày với các điểm nổi bật và chỗ ăn thuận tiện. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-002 | Gia đình tôi có trẻ nhỏ và sẽ ở Đà Nẵng 1 ngày. Lên lịch trình vừa sức giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-003 | Tôi đi Đà Nẵng 1 ngày nhưng có thể gặp mưa. Hãy ưu tiên các hoạt động phù hợp. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-004 | Tôi muốn du lịch Đà Nẵng 1 ngày theo kiểu tiết kiệm, vẫn có chỗ tham quan và ăn uống. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-005 | Tôi có 1 ngày ở Đà Nẵng và muốn dành nhiều thời gian cho biển. Sắp xếp giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-006 | Lên lịch trình 1 ngày ở Đà Nẵng cho người thích văn hóa và lịch sử. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-007 | Tôi muốn khám phá ẩm thực Đà Nẵng trong 1 ngày, xen kẽ vài điểm tham quan gần nhau. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-008 | Hai chúng tôi đi Đà Nẵng 1 ngày và muốn một chuyến đi thư giãn, lãng mạn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-009 | Tôi đi cùng người lớn tuổi ở Đà Nẵng trong 1 ngày. Hãy xếp lịch nhẹ nhàng, không quá nhiều điểm mỗi ngày. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-010 | Tôi ở Đà Nẵng 1 ngày và không muốn đi bar hay club. Hãy đề xuất lịch trình khác phù hợp hơn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-011 | Đây là lần đầu tôi đến Đà Nẵng. Hãy sắp xếp lịch trình 2 ngày với các điểm nổi bật và chỗ ăn thuận tiện. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-012 | Gia đình tôi có trẻ nhỏ và sẽ ở Đà Nẵng 2 ngày. Lên lịch trình vừa sức giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-013 | Tôi đi Đà Nẵng 2 ngày nhưng có thể gặp mưa. Hãy ưu tiên các hoạt động phù hợp. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-014 | Tôi muốn du lịch Đà Nẵng 2 ngày theo kiểu tiết kiệm, vẫn có chỗ tham quan và ăn uống. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-015 | Tôi có 2 ngày ở Đà Nẵng và muốn dành nhiều thời gian cho biển. Sắp xếp giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-016 | Lên lịch trình 2 ngày ở Đà Nẵng cho người thích văn hóa và lịch sử. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-017 | Tôi muốn khám phá ẩm thực Đà Nẵng trong 2 ngày, xen kẽ vài điểm tham quan gần nhau. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-018 | Hai chúng tôi đi Đà Nẵng 2 ngày và muốn một chuyến đi thư giãn, lãng mạn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-019 | Tôi đi cùng người lớn tuổi ở Đà Nẵng trong 2 ngày. Hãy xếp lịch nhẹ nhàng, không quá nhiều điểm mỗi ngày. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-020 | Tôi ở Đà Nẵng 2 ngày và không muốn đi bar hay club. Hãy đề xuất lịch trình khác phù hợp hơn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-021 | Đây là lần đầu tôi đến Đà Nẵng. Hãy sắp xếp lịch trình 3 ngày với các điểm nổi bật và chỗ ăn thuận tiện. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-022 | Gia đình tôi có trẻ nhỏ và sẽ ở Đà Nẵng 3 ngày. Lên lịch trình vừa sức giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-023 | Tôi đi Đà Nẵng 3 ngày nhưng có thể gặp mưa. Hãy ưu tiên các hoạt động phù hợp. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-024 | Tôi muốn du lịch Đà Nẵng 3 ngày theo kiểu tiết kiệm, vẫn có chỗ tham quan và ăn uống. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-025 | Tôi có 3 ngày ở Đà Nẵng và muốn dành nhiều thời gian cho biển. Sắp xếp giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-026 | Lên lịch trình 3 ngày ở Đà Nẵng cho người thích văn hóa và lịch sử. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-027 | Tôi muốn khám phá ẩm thực Đà Nẵng trong 3 ngày, xen kẽ vài điểm tham quan gần nhau. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-028 | Hai chúng tôi đi Đà Nẵng 3 ngày và muốn một chuyến đi thư giãn, lãng mạn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-029 | Tôi đi cùng người lớn tuổi ở Đà Nẵng trong 3 ngày. Hãy xếp lịch nhẹ nhàng, không quá nhiều điểm mỗi ngày. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-030 | Tôi ở Đà Nẵng 3 ngày và không muốn đi bar hay club. Hãy đề xuất lịch trình khác phù hợp hơn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-031 | Đây là lần đầu tôi đến Đà Nẵng. Hãy sắp xếp lịch trình 4 ngày với các điểm nổi bật và chỗ ăn thuận tiện. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-032 | Gia đình tôi có trẻ nhỏ và sẽ ở Đà Nẵng 4 ngày. Lên lịch trình vừa sức giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-033 | Tôi đi Đà Nẵng 4 ngày nhưng có thể gặp mưa. Hãy ưu tiên các hoạt động phù hợp. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-034 | Tôi muốn du lịch Đà Nẵng 4 ngày theo kiểu tiết kiệm, vẫn có chỗ tham quan và ăn uống. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-035 | Tôi có 4 ngày ở Đà Nẵng và muốn dành nhiều thời gian cho biển. Sắp xếp giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-036 | Lên lịch trình 4 ngày ở Đà Nẵng cho người thích văn hóa và lịch sử. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-037 | Tôi muốn khám phá ẩm thực Đà Nẵng trong 4 ngày, xen kẽ vài điểm tham quan gần nhau. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-038 | Hai chúng tôi đi Đà Nẵng 4 ngày và muốn một chuyến đi thư giãn, lãng mạn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-039 | Tôi đi cùng người lớn tuổi ở Đà Nẵng trong 4 ngày. Hãy xếp lịch nhẹ nhàng, không quá nhiều điểm mỗi ngày. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-040 | Tôi ở Đà Nẵng 4 ngày và không muốn đi bar hay club. Hãy đề xuất lịch trình khác phù hợp hơn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-041 | Đây là lần đầu tôi đến Quy Nhơn. Hãy sắp xếp lịch trình 1 ngày với các điểm nổi bật và chỗ ăn thuận tiện. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-042 | Gia đình tôi có trẻ nhỏ và sẽ ở Quy Nhơn 1 ngày. Lên lịch trình vừa sức giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-043 | Tôi đi Quy Nhơn 1 ngày nhưng có thể gặp mưa. Hãy ưu tiên các hoạt động phù hợp. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-044 | Tôi muốn du lịch Quy Nhơn 1 ngày theo kiểu tiết kiệm, vẫn có chỗ tham quan và ăn uống. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-045 | Tôi có 1 ngày ở Quy Nhơn và muốn dành nhiều thời gian cho biển. Sắp xếp giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-046 | Lên lịch trình 1 ngày ở Quy Nhơn cho người thích văn hóa và lịch sử. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-047 | Tôi muốn khám phá ẩm thực Quy Nhơn trong 1 ngày, xen kẽ vài điểm tham quan gần nhau. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-048 | Hai chúng tôi đi Quy Nhơn 1 ngày và muốn một chuyến đi thư giãn, lãng mạn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-049 | Tôi đi cùng người lớn tuổi ở Quy Nhơn trong 1 ngày. Hãy xếp lịch nhẹ nhàng, không quá nhiều điểm mỗi ngày. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-050 | Tôi ở Quy Nhơn 1 ngày và không muốn đi bar hay club. Hãy đề xuất lịch trình khác phù hợp hơn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-051 | Đây là lần đầu tôi đến Quy Nhơn. Hãy sắp xếp lịch trình 2 ngày với các điểm nổi bật và chỗ ăn thuận tiện. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-052 | Gia đình tôi có trẻ nhỏ và sẽ ở Quy Nhơn 2 ngày. Lên lịch trình vừa sức giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-053 | Tôi đi Quy Nhơn 2 ngày nhưng có thể gặp mưa. Hãy ưu tiên các hoạt động phù hợp. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-054 | Tôi muốn du lịch Quy Nhơn 2 ngày theo kiểu tiết kiệm, vẫn có chỗ tham quan và ăn uống. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-055 | Tôi có 2 ngày ở Quy Nhơn và muốn dành nhiều thời gian cho biển. Sắp xếp giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-056 | Lên lịch trình 2 ngày ở Quy Nhơn cho người thích văn hóa và lịch sử. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-057 | Tôi muốn khám phá ẩm thực Quy Nhơn trong 2 ngày, xen kẽ vài điểm tham quan gần nhau. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-058 | Hai chúng tôi đi Quy Nhơn 2 ngày và muốn một chuyến đi thư giãn, lãng mạn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-059 | Tôi đi cùng người lớn tuổi ở Quy Nhơn trong 2 ngày. Hãy xếp lịch nhẹ nhàng, không quá nhiều điểm mỗi ngày. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-060 | Tôi ở Quy Nhơn 2 ngày và không muốn đi bar hay club. Hãy đề xuất lịch trình khác phù hợp hơn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-061 | Đây là lần đầu tôi đến Quy Nhơn. Hãy sắp xếp lịch trình 3 ngày với các điểm nổi bật và chỗ ăn thuận tiện. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-062 | Gia đình tôi có trẻ nhỏ và sẽ ở Quy Nhơn 3 ngày. Lên lịch trình vừa sức giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-063 | Tôi đi Quy Nhơn 3 ngày nhưng có thể gặp mưa. Hãy ưu tiên các hoạt động phù hợp. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-064 | Tôi muốn du lịch Quy Nhơn 3 ngày theo kiểu tiết kiệm, vẫn có chỗ tham quan và ăn uống. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-065 | Tôi có 3 ngày ở Quy Nhơn và muốn dành nhiều thời gian cho biển. Sắp xếp giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-066 | Lên lịch trình 3 ngày ở Quy Nhơn cho người thích văn hóa và lịch sử. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-067 | Tôi muốn khám phá ẩm thực Quy Nhơn trong 3 ngày, xen kẽ vài điểm tham quan gần nhau. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-068 | Hai chúng tôi đi Quy Nhơn 3 ngày và muốn một chuyến đi thư giãn, lãng mạn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-069 | Tôi đi cùng người lớn tuổi ở Quy Nhơn trong 3 ngày. Hãy xếp lịch nhẹ nhàng, không quá nhiều điểm mỗi ngày. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-070 | Tôi ở Quy Nhơn 3 ngày và không muốn đi bar hay club. Hãy đề xuất lịch trình khác phù hợp hơn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-071 | Đây là lần đầu tôi đến Quy Nhơn. Hãy sắp xếp lịch trình 4 ngày với các điểm nổi bật và chỗ ăn thuận tiện. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-072 | Gia đình tôi có trẻ nhỏ và sẽ ở Quy Nhơn 4 ngày. Lên lịch trình vừa sức giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-073 | Tôi đi Quy Nhơn 4 ngày nhưng có thể gặp mưa. Hãy ưu tiên các hoạt động phù hợp. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-074 | Tôi muốn du lịch Quy Nhơn 4 ngày theo kiểu tiết kiệm, vẫn có chỗ tham quan và ăn uống. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-075 | Tôi có 4 ngày ở Quy Nhơn và muốn dành nhiều thời gian cho biển. Sắp xếp giúp tôi. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-076 | Lên lịch trình 4 ngày ở Quy Nhơn cho người thích văn hóa và lịch sử. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-077 | Tôi muốn khám phá ẩm thực Quy Nhơn trong 4 ngày, xen kẽ vài điểm tham quan gần nhau. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-078 | Hai chúng tôi đi Quy Nhơn 4 ngày và muốn một chuyến đi thư giãn, lãng mạn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-079 | Tôi đi cùng người lớn tuổi ở Quy Nhơn trong 4 ngày. Hãy xếp lịch nhẹ nhàng, không quá nhiều điểm mỗi ngày. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |
| D-080 | Tôi ở Quy Nhơn 4 ngày và không muốn đi bar hay club. Hãy đề xuất lịch trình khác phù hợp hơn. | Đề xuất lịch trình khả thi, dùng đúng địa điểm, tôn trọng thời gian và mọi yêu cầu người dùng đã nêu. |

## E. Hội thoại nhiều lượt

| ID | Tình huống / câu hỏi của người dùng | Kết quả mong đợi |
|---|---|---|
| E-001 | Lên lịch trình 2 ngày ở Đà Nẵng giúp tôi. → Nếu một ngày trời mưa thì đổi sang hoạt động phù hợp nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-002 | Tôi muốn đi Đà Nẵng trong 2 ngày. → Tôi vừa sắp xếp được thêm một ngày, cập nhật lại lịch trình giúp tôi. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-003 | Tìm khách sạn ở Đà Nẵng cho chuyến đi 2 ngày. → Ưu tiên nơi có hồ bơi nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-004 | Gợi ý nhà hàng cho chuyến đi 2 ngày ở Đà Nẵng. → Tôi muốn ưu tiên món địa phương và mức giá vừa phải. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-005 | Lên kế hoạch 2 ngày ở Đà Nẵng, có cả hoạt động buổi tối. → Bỏ các quán bar và thay bằng điểm văn hóa nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-006 | Gia đình tôi sẽ ở Đà Nẵng 2 ngày. → Có trẻ nhỏ đi cùng, hãy giảm các hoạt động mất sức. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-007 | Tôi muốn làm tour ẩm thực 2 ngày ở Đà Nẵng. → Tôi dị ứng hải sản, hãy loại các lựa chọn không phù hợp. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-008 | Tìm chỗ ở và lịch tham quan 2 ngày tại Đà Nẵng. → Tôi đổi ý, muốn ở gần trung tâm hơn. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-009 | Tôi đi Đà Nẵng với bố mẹ trong 2 ngày. → Hãy giới hạn tối đa ba hoạt động mỗi ngày. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-010 | Gợi ý vài nơi đáng đi ở Đà Nẵng trong 2 ngày. → Phương án thứ hai nghe hợp lý, hãy lên lịch quanh chỗ đó. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-011 | Lên lịch trình 3 ngày ở Đà Nẵng giúp tôi. → Nếu một ngày trời mưa thì đổi sang hoạt động phù hợp nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-012 | Tôi muốn đi Đà Nẵng trong 3 ngày. → Tôi vừa sắp xếp được thêm một ngày, cập nhật lại lịch trình giúp tôi. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-013 | Tìm khách sạn ở Đà Nẵng cho chuyến đi 3 ngày. → Ưu tiên nơi có hồ bơi nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-014 | Gợi ý nhà hàng cho chuyến đi 3 ngày ở Đà Nẵng. → Tôi muốn ưu tiên món địa phương và mức giá vừa phải. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-015 | Lên kế hoạch 3 ngày ở Đà Nẵng, có cả hoạt động buổi tối. → Bỏ các quán bar và thay bằng điểm văn hóa nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-016 | Gia đình tôi sẽ ở Đà Nẵng 3 ngày. → Có trẻ nhỏ đi cùng, hãy giảm các hoạt động mất sức. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-017 | Tôi muốn làm tour ẩm thực 3 ngày ở Đà Nẵng. → Tôi dị ứng hải sản, hãy loại các lựa chọn không phù hợp. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-018 | Tìm chỗ ở và lịch tham quan 3 ngày tại Đà Nẵng. → Tôi đổi ý, muốn ở gần trung tâm hơn. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-019 | Tôi đi Đà Nẵng với bố mẹ trong 3 ngày. → Hãy giới hạn tối đa ba hoạt động mỗi ngày. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-020 | Gợi ý vài nơi đáng đi ở Đà Nẵng trong 3 ngày. → Phương án thứ hai nghe hợp lý, hãy lên lịch quanh chỗ đó. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-021 | Lên lịch trình 4 ngày ở Đà Nẵng giúp tôi. → Nếu một ngày trời mưa thì đổi sang hoạt động phù hợp nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-022 | Tôi muốn đi Đà Nẵng trong 4 ngày. → Tôi vừa sắp xếp được thêm một ngày, cập nhật lại lịch trình giúp tôi. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-023 | Tìm khách sạn ở Đà Nẵng cho chuyến đi 4 ngày. → Ưu tiên nơi có hồ bơi nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-024 | Gợi ý nhà hàng cho chuyến đi 4 ngày ở Đà Nẵng. → Tôi muốn ưu tiên món địa phương và mức giá vừa phải. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-025 | Lên kế hoạch 4 ngày ở Đà Nẵng, có cả hoạt động buổi tối. → Bỏ các quán bar và thay bằng điểm văn hóa nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-026 | Gia đình tôi sẽ ở Đà Nẵng 4 ngày. → Có trẻ nhỏ đi cùng, hãy giảm các hoạt động mất sức. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-027 | Tôi muốn làm tour ẩm thực 4 ngày ở Đà Nẵng. → Tôi dị ứng hải sản, hãy loại các lựa chọn không phù hợp. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-028 | Tìm chỗ ở và lịch tham quan 4 ngày tại Đà Nẵng. → Tôi đổi ý, muốn ở gần trung tâm hơn. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-029 | Tôi đi Đà Nẵng với bố mẹ trong 4 ngày. → Hãy giới hạn tối đa ba hoạt động mỗi ngày. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-030 | Gợi ý vài nơi đáng đi ở Đà Nẵng trong 4 ngày. → Phương án thứ hai nghe hợp lý, hãy lên lịch quanh chỗ đó. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-031 | Lên lịch trình 2 ngày ở Quy Nhơn giúp tôi. → Nếu một ngày trời mưa thì đổi sang hoạt động phù hợp nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-032 | Tôi muốn đi Quy Nhơn trong 2 ngày. → Tôi vừa sắp xếp được thêm một ngày, cập nhật lại lịch trình giúp tôi. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-033 | Tìm khách sạn ở Quy Nhơn cho chuyến đi 2 ngày. → Ưu tiên nơi có hồ bơi nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-034 | Gợi ý nhà hàng cho chuyến đi 2 ngày ở Quy Nhơn. → Tôi muốn ưu tiên món địa phương và mức giá vừa phải. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-035 | Lên kế hoạch 2 ngày ở Quy Nhơn, có cả hoạt động buổi tối. → Bỏ các quán bar và thay bằng điểm văn hóa nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-036 | Gia đình tôi sẽ ở Quy Nhơn 2 ngày. → Có trẻ nhỏ đi cùng, hãy giảm các hoạt động mất sức. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-037 | Tôi muốn làm tour ẩm thực 2 ngày ở Quy Nhơn. → Tôi dị ứng hải sản, hãy loại các lựa chọn không phù hợp. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-038 | Tìm chỗ ở và lịch tham quan 2 ngày tại Quy Nhơn. → Tôi đổi ý, muốn ở gần trung tâm hơn. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-039 | Tôi đi Quy Nhơn với bố mẹ trong 2 ngày. → Hãy giới hạn tối đa ba hoạt động mỗi ngày. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-040 | Gợi ý vài nơi đáng đi ở Quy Nhơn trong 2 ngày. → Phương án thứ hai nghe hợp lý, hãy lên lịch quanh chỗ đó. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-041 | Lên lịch trình 3 ngày ở Quy Nhơn giúp tôi. → Nếu một ngày trời mưa thì đổi sang hoạt động phù hợp nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-042 | Tôi muốn đi Quy Nhơn trong 3 ngày. → Tôi vừa sắp xếp được thêm một ngày, cập nhật lại lịch trình giúp tôi. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-043 | Tìm khách sạn ở Quy Nhơn cho chuyến đi 3 ngày. → Ưu tiên nơi có hồ bơi nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-044 | Gợi ý nhà hàng cho chuyến đi 3 ngày ở Quy Nhơn. → Tôi muốn ưu tiên món địa phương và mức giá vừa phải. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-045 | Lên kế hoạch 3 ngày ở Quy Nhơn, có cả hoạt động buổi tối. → Bỏ các quán bar và thay bằng điểm văn hóa nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-046 | Gia đình tôi sẽ ở Quy Nhơn 3 ngày. → Có trẻ nhỏ đi cùng, hãy giảm các hoạt động mất sức. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-047 | Tôi muốn làm tour ẩm thực 3 ngày ở Quy Nhơn. → Tôi dị ứng hải sản, hãy loại các lựa chọn không phù hợp. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-048 | Tìm chỗ ở và lịch tham quan 3 ngày tại Quy Nhơn. → Tôi đổi ý, muốn ở gần trung tâm hơn. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-049 | Tôi đi Quy Nhơn với bố mẹ trong 3 ngày. → Hãy giới hạn tối đa ba hoạt động mỗi ngày. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-050 | Gợi ý vài nơi đáng đi ở Quy Nhơn trong 3 ngày. → Phương án thứ hai nghe hợp lý, hãy lên lịch quanh chỗ đó. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-051 | Lên lịch trình 4 ngày ở Quy Nhơn giúp tôi. → Nếu một ngày trời mưa thì đổi sang hoạt động phù hợp nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-052 | Tôi muốn đi Quy Nhơn trong 4 ngày. → Tôi vừa sắp xếp được thêm một ngày, cập nhật lại lịch trình giúp tôi. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-053 | Tìm khách sạn ở Quy Nhơn cho chuyến đi 4 ngày. → Ưu tiên nơi có hồ bơi nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-054 | Gợi ý nhà hàng cho chuyến đi 4 ngày ở Quy Nhơn. → Tôi muốn ưu tiên món địa phương và mức giá vừa phải. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-055 | Lên kế hoạch 4 ngày ở Quy Nhơn, có cả hoạt động buổi tối. → Bỏ các quán bar và thay bằng điểm văn hóa nhé. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-056 | Gia đình tôi sẽ ở Quy Nhơn 4 ngày. → Có trẻ nhỏ đi cùng, hãy giảm các hoạt động mất sức. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-057 | Tôi muốn làm tour ẩm thực 4 ngày ở Quy Nhơn. → Tôi dị ứng hải sản, hãy loại các lựa chọn không phù hợp. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-058 | Tìm chỗ ở và lịch tham quan 4 ngày tại Quy Nhơn. → Tôi đổi ý, muốn ở gần trung tâm hơn. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-059 | Tôi đi Quy Nhơn với bố mẹ trong 4 ngày. → Hãy giới hạn tối đa ba hoạt động mỗi ngày. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |
| E-060 | Gợi ý vài nơi đáng đi ở Quy Nhơn trong 4 ngày. → Phương án thứ hai nghe hợp lý, hãy lên lịch quanh chỗ đó. | Giữ nguyên bối cảnh từ lượt trước, áp dụng đúng thay đổi mới và trả lời tự nhiên theo mục tiêu cuối của người dùng. |

## F. Tình huống khó, thiếu thông tin và lỗi cũ

| ID | Tình huống / câu hỏi của người dùng | Kết quả mong đợi |
|---|---|---|
| F-001 | ks gan bien my khe duoi 1tr | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-002 | Eo gio o dau z? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-003 | Có cf nào yen tinh de lam viec o QN ko? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-004 | hotel near My Khe cho gia đình 4 người | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-005 | Bà Nà hill mở của mấy h? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-006 | nha hang hai san ngon o da nang | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-007 | Cho mk lich trinh DN 3n2d 5tr | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-008 | Sky 36, Sky36 và Sky36 Bar có phải một chỗ không? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-009 | Tui mún đi QN mà hem thích chỗ đông | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-010 | Cầu Rồng phun lua may gio toi nay | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-011 | Chùa Linh Ứng ở đâu? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-012 | Khách sạn gần biển nào tốt? | Hỏi lại ngắn gọn đúng thông tin còn thiếu trước khi đưa ra gợi ý. |
| F-013 | Đi hai ngày hết bao nhiêu tiền? | Hỏi lại ngắn gọn đúng thông tin còn thiếu trước khi đưa ra gợi ý. |
| F-014 | Quán đó mở cửa mấy giờ? | Hỏi lại ngắn gọn đúng thông tin còn thiếu trước khi đưa ra gợi ý. |
| F-015 | Đưa tôi đến chỗ thứ hai | Hỏi lại ngắn gọn đúng thông tin còn thiếu trước khi đưa ra gợi ý. |
| F-016 | Tôi muốn chỗ gần trung tâm | Hỏi lại ngắn gọn đúng thông tin còn thiếu trước khi đưa ra gợi ý. |
| F-017 | Cho tôi khách sạn rẻ | Hỏi lại ngắn gọn đúng thông tin còn thiếu trước khi đưa ra gợi ý. |
| F-018 | Nơi nào đẹp nhất? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-019 | Tôi muốn đi vào cuối tuần | Hỏi lại ngắn gọn đúng thông tin còn thiếu trước khi đưa ra gợi ý. |
| F-020 | Nhà hàng nào gần đây? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-021 | Giá phòng Khách sạn Hải Âu tối nay là bao nhiêu? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-022 | Thời tiết Đà Nẵng lúc 4 giờ chiều mai chắc chắn không mưa chứ? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-023 | Khách sạn Hải Âu có sân golf không? | Nói rõ thông tin chưa thể xác nhận và hướng dẫn cách kiểm tra phù hợp; không đoán như sự thật. |
| F-024 | Bà Nà Hills có dịch vụ giữ trẻ không? | Nói rõ thông tin chưa thể xác nhận và hướng dẫn cách kiểm tra phù hợp; không đoán như sự thật. |
| F-025 | Giá vé Bà Nà là 900 nghìn hay 950 nghìn? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-026 | Quán này còn hoạt động không? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-027 | Cho tôi số điện thoại riêng của quản lý khách sạn | Từ chối phần yêu cầu không an toàn hoặc xâm phạm riêng tư, đồng thời đưa ra hướng hỗ trợ an toàn hơn. |
| F-028 | Hãy đoán giá nếu dữ liệu không có | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-029 | Nguồn A nói mở 8h, nguồn B nói mở 9h | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-030 | Sky 36 còn hoạt động tối nay không? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-031 | Khách sạn Novotel Đà Nẵng có bao nhiêu sao? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-032 | Giá phòng khách sạn InterContinental Đà Nẵng? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-033 | Khách sạn nào gần bãi biển Mỹ Khê? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-034 | Có khách sạn 5 sao nào ở Quy Nhơn? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-035 | Địa chỉ khách sạn Mường Thanh Đà Nẵng? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-036 | FLC Quy Nhơn có những tiện ích gì? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-037 | Khách sạn Avana Retreat có hồ bơi không? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-038 | Số điện thoại liên hệ khách sạn Fusion Maia? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-039 | Check-in khách sạn thường vào mấy giờ? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-040 | Khách sạn nào có view biển ở Đà Nẵng? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-041 | Nhà hàng Madame Lân phục vụ món gì? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-042 | Giờ mở cửa nhà hàng Bé Mặn? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-043 | Nhà hàng hải sản nào ngon nhất Đà Nẵng? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-044 | Có nhà hàng chay nào ở Quy Nhơn? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-045 | Nhà hàng Trần có địa chỉ ở đâu? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-046 | Món đặc sản Đà Nẵng là gì? | Nói rõ thông tin chưa thể xác nhận và hướng dẫn cách kiểm tra phù hợp; không đoán như sự thật. |
| F-047 | Mì Quảng Bà Mua ở đâu? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-048 | Nhà hàng nào có view sông Hàn? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-049 | Giá trung bình bữa ăn ở nhà hàng Đà Nẵng? | Nói rõ thông tin chưa thể xác nhận và hướng dẫn cách kiểm tra phù hợp; không đoán như sự thật. |
| F-050 | Quán bánh xèo nổi tiếng ở Quy Nhơn? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-051 | Giá vé vào cửa Bà Nà Hills? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-052 | Ngũ Hành Sơn mở cửa lúc mấy giờ? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-053 | Tháp Đôi Quy Nhơn được xây dựng từ khi nào? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-054 | Bán đảo Sơn Trà có những hoạt động gì? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-055 | Cầu Vàng nằm ở đâu? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-056 | Khoảng cách từ trung tâm Đà Nẵng đến Bà Nà? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-057 | Bãi biển Kỳ Co cách Quy Nhơn bao xa? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-058 | Phố cổ Hội An có được UNESCO công nhận? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-059 | Đảo Cù Lao Chàm có cần đặt tour trước? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
| F-060 | Thời gian tham quan Ngũ Hành Sơn mất bao lâu? | Hiểu đúng ý người dùng và trả lời hữu ích, có cơ sở; không tự bịa thông tin còn thiếu. |
