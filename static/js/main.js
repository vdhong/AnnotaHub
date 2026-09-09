/* AnnotaHub Main JavaScript */

// Tự đóng THÔNG BÁO CHỚP (django messages) sau 5 giây.
//
// Chỉ nhắm vào .alert-dismissible — đó là khuôn mà base.html dùng cho messages.
// Trước đây quét toàn bộ .alert nên xoá luôn những khung thông tin cố định:
// banner "dự án đang khoá", ô "AI đề xuất … Đồng ý với AI", các gợi ý ở trang
// danh sách… Người dùng thấy chúng hiện ra rồi biến mất sau vài giây.
document.addEventListener('DOMContentLoaded', function() {
    const alerts = document.querySelectorAll('.alert-dismissible');
    alerts.forEach(function(alert) {
        setTimeout(function() {
            const bsAlert = new bootstrap.Alert(alert);
            bsAlert.close();
        }, 5000);
    });
});

// Confirm before destructive actions
function confirmAction(message) {
    return confirm(message || 'Are you sure you want to perform this action?');
}

// Utility: format number with commas
function formatNumber(num) {
    return num.toString().replace(/\B(?=(\d{3})+(?!\d))/g, ",");
}

// Utility: time ago
function timeAgo(date) {
    const seconds = Math.floor((new Date() - new Date(date)) / 1000);
    if (seconds < 60) return 'just now';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + ' min ago';
    const hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + ' hours ago';
    const days = Math.floor(hours / 24);
    return days + ' days ago';
}

console.log('AnnotaHub loaded successfully');